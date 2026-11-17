import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.dashboard import render_pages, tickets_json  # noqa: E402
from tests.test_dashboard_lanes import Browser  # noqa: E402
from tests.test_dashboard_layout import _snapshot  # noqa: E402
from tests.test_dashboard_ticket_detail import RECORD, _quote, _with_shipped  # noqa: E402

PROBE = """(function () {
  var art = document.querySelector('article.ticketpage');
  return {ticket: art && art.getAttribute('data-ticket'), h1: [].map.call(document.querySelectorAll('h1'), function (h) { return h.textContent; }),
          sections: [].map.call(document.querySelectorAll('.ticketpage .jd-panel h2 .t, .ticketpage .jd-chart summary'), function (n) { return n.firstChild.textContent.trim(); }),
          scrollWidth: document.documentElement.scrollWidth};
})()"""


def _page_with_stubs(snapshot: dict) -> str:
    shim = (
        "<script>window.fetch = function (url) { var body = url === 'tickets.json' ? "
        f"{tickets_json(snapshot).strip()} : {json.dumps(snapshot)};"
        " return Promise.resolve({ok: true, status: 200, json: function () { return Promise.resolve(body); }}); };</script>"
    )
    return render_pages(snapshot)["ticket.html"].replace("<script>", shim + "<script>", 1)


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        snapshot = _with_shipped(_snapshot(Path(tmp)))
        html = _page_with_stubs(snapshot)
    chrome = Browser()
    try:
        for kind, address in RECORD.items():
            for width in (1440, 375):
                chrome.open(html, width)
                chrome.eval(f"""new Promise(function (done) {{
                  location.hash = {json.dumps("t=" + _quote(address))};
                  (function wait() {{ if (document.querySelector('article.ticketpage canvas.jd-journey')) done(true); else setTimeout(wait, 50); }})();
                }})""")
                print(kind, width, json.dumps(chrome.eval(PROBE)))
                out = ROOT / "docs" / "screenshots" / f"issue-655-ticket-{kind}-{width}.png"
                chrome.screenshot(out, width)
                print(out)
    finally:
        chrome.close()


if __name__ == "__main__":
    main()
