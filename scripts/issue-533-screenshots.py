import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.dashboard import render_pages, tickets_json  # noqa: E402
from tests.test_dashboard_lanes import Browser  # noqa: E402
from tests.test_dashboard_layout import _snapshot  # noqa: E402
from tests.test_dashboard_ticket_detail import LIVE, _quote, _with_shipped  # noqa: E402

PROBE = """(function () {
  var art = document.querySelector('article.ticketpage');
  var secs = [].map.call(art.querySelectorAll('.jd-hero, [data-sec]'), function (n) {
    return [n.getAttribute('data-sec') || 'hero', n.getBoundingClientRect().top];
  }).sort(function (a, b) { return a[1] - b[1]; });
  return {ticket: art.getAttribute('data-ticket'), order: secs.map(function (s) { return s[0]; }),
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
        html = _page_with_stubs(_with_shipped(_snapshot(Path(tmp))))
    address = LIVE["running"][0]
    chrome = Browser()
    try:
        for width in (1440, 375):
            chrome.open(html, width)
            chrome.eval(f"""new Promise(function (done) {{
              location.hash = {json.dumps("t=" + _quote(address))};
              (function wait() {{ if (document.querySelector('article.ticketpage canvas.jd-journey')) setTimeout(done, 3000, true); else setTimeout(wait, 50); }})();
            }})""")
            print(width, json.dumps(chrome.eval(PROBE)))
            out = ROOT / "docs" / "screenshots" / f"issue-533-ticket-{width}.png"
            chrome.screenshot(out, width)
            print(out)
    finally:
        chrome.close()


if __name__ == "__main__":
    main()
