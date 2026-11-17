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
  var sec = document.querySelector('article.ticketpage [data-sec=journey]');
  var canvas = sec.querySelector('canvas.jd-journey'), box = canvas.closest('.jd-box');
  return {canvas: [canvas.width, canvas.height], box: [box.clientWidth, box.scrollWidth],
          subtitle: sec.querySelector('h2').innerText, scrollWidth: document.documentElement.scrollWidth};
})()"""

PHONE_HEIGHT = 812


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
            chrome.open(html, width, PHONE_HEIGHT if width < 700 else 900)
            chrome.eval(f"""new Promise(function (done) {{
              location.hash = {json.dumps("t=" + _quote(address))};
              (function wait() {{ if (document.querySelector('article.ticketpage canvas.jd-journey')) setTimeout(done, 3000, true); else setTimeout(wait, 50); }})();
            }})""")
            phone = width < 700
            if phone:
                chrome.eval("document.querySelector('article.ticketpage [data-sec=journey]').scrollIntoView({block: 'start'})")
            print(width, json.dumps(chrome.eval(PROBE)))
            out = ROOT / "docs" / "screenshots" / f"issue-693-ticket-{width}.png"
            chrome.screenshot(out, width, full_page=not phone)
            print(out)
    finally:
        chrome.close()


if __name__ == "__main__":
    main()
