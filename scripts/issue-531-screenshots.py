import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.dashboard_pages.common import ticket_page_href  # noqa: E402
from tests.test_dashboard_lanes import Browser, _page_with_status_stub  # noqa: E402
from tests.test_dashboard_layout import _snapshot  # noqa: E402

SHOTS = {"ticket": ("running", "h-review"), "ticket-queued": ("queued", "q-claude")}

PROBE = """(function () {
  var art = document.querySelector('article.ticketpage');
  return {ticket: art && art.getAttribute('data-ticket'), h1: [].map.call(document.querySelectorAll('h1'), function (h) { return h.textContent; }),
          sections: [].map.call(document.querySelectorAll('.ticketpage .jd-panel h2 .t, .ticketpage .jd-chart summary'), function (n) { return n.firstChild.textContent.trim(); }),
          scrollWidth: document.documentElement.scrollWidth};
})()"""


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        snapshot = _snapshot(Path(tmp))
        html = _page_with_status_stub(snapshot, "ticket.html")
    chrome = Browser()
    try:
        for name, (kind, job) in SHOTS.items():
            row = next(r for r in snapshot[kind] if r["job"] == job)
            fragment = ticket_page_href(row).split("#", 1)[1]
            for width in (1440, 375):
                chrome.open(html, width)
                chrome.eval(f"""new Promise(function (done) {{
                  location.hash = {json.dumps(fragment)};
                  (function wait() {{ if (document.querySelector('article.ticketpage canvas.jd-journey')) done(true); else setTimeout(wait, 50); }})();
                }})""")
                print(name, width, json.dumps(chrome.eval(PROBE)))
                out = ROOT / "docs" / "screenshots" / f"issue-531-{name}-{width}.png"
                chrome.screenshot(out, width)
                print(out)
    finally:
        chrome.close()


if __name__ == "__main__":
    main()
