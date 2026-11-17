import base64
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests.test_dashboard_system as system  # noqa: E402
from lib.dashboard import render_pages  # noqa: E402
from tests.test_dashboard_tickets_board import shelf_snapshot  # noqa: E402

MEASURE = (
    "(() => { const shelf = document.getElementById('shipped');"
    " const newest = shelf.querySelector('.tk-shelf > .tk-tiles');"
    " const drawer = shelf.querySelector('details.tk-earlier');"
    " return JSON.stringify({viewport: innerWidth, page: document.documentElement.scrollWidth,"
    " fits: document.documentElement.scrollWidth <= innerWidth,"
    " count: shelf.querySelector('h2 .count').textContent, sub: shelf.querySelector('.tk-sub').textContent,"
    " newest: newest.querySelectorAll('.tk-tile').length,"
    " first: newest.querySelector('.tk-tile').title,"
    " drawer: [drawer.open, drawer.querySelector('summary').textContent],"
    " height: Math.ceil(shelf.getBoundingClientRect().bottom + scrollY) + 16}); })()"
)


def main() -> None:
    if len(sys.argv) > 1:
        system.CHROME = sys.argv[1]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        page = Path(tmp) / "job.html"
        page.write_text(render_pages(shelf_snapshot(Path(tmp)))["job.html"], encoding="utf-8")
        chrome = system._Chrome(Path(tmp) / "profile")
        try:
            for width in (1440, 375):
                chrome.send("Emulation.setDeviceMetricsOverride", chrome.session,
                            width=width, height=900, deviceScaleFactor=1, mobile=False)
                chrome.send("Page.navigate", chrome.session, url=page.as_uri())
                while chrome.run("document.readyState") != "complete":
                    time.sleep(0.05)
                time.sleep(0.5)
                found = json.loads(chrome.run(MEASURE))
                print(width, found)
                assert found["newest"] == 10, "ten tiles in the newest row"
                assert found["drawer"] == [False, "60 earlier"], "the earlier drawer starts closed"
                if width == 375:
                    assert found["fits"], "nothing scrolls sideways at 375"
                chrome.send("Emulation.setDeviceMetricsOverride", chrome.session,
                            width=width, height=found["height"], deviceScaleFactor=1, mobile=False)
                time.sleep(0.5)
                clip = {"x": 0, "y": 0, "width": width, "height": found["height"], "scale": 1}
                shot = chrome.send("Page.captureScreenshot", chrome.session, format="png", clip=clip)
                out = ROOT / "docs" / "screenshots" / f"issue-671-tickets-{width}.png"
                out.write_bytes(base64.b64decode(shot["data"]))
                print(out)
        finally:
            chrome.close()


if __name__ == "__main__":
    main()
