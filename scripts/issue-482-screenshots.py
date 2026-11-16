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
from tests.test_dashboard_tickets_board import _troubled, board_snapshot  # noqa: E402

MEASURE = (
    "(() => { document.getElementById('pipeline').hidden = true; document.getElementById('tickets').hidden = false;"
    " const p = document.getElementById('tickets').getBoundingClientRect();"
    " const w = document.querySelector('#tickets .tk-scroll');"
    " return JSON.stringify({viewport: innerWidth, page: document.documentElement.scrollWidth,"
    " panel: Math.round(p.width), panelRight: Math.round(p.right),"
    " wrapper: w.clientWidth, table: w.scrollWidth, overflowX: getComputedStyle(w).overflowX}); })()"
)


def main() -> None:
    if len(sys.argv) > 1:
        system.CHROME = sys.argv[1]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        snapshot, _ = _troubled(board_snapshot(Path(tmp)))
        page = Path(tmp) / "job.html"
        page.write_text(render_pages(snapshot)["job.html"], encoding="utf-8")
        chrome = system._Chrome(Path(tmp) / "profile")
        try:
            for width, height in ((1440, 1600), (375, 2600)):
                chrome.send("Emulation.setDeviceMetricsOverride", chrome.session,
                            width=width, height=height, deviceScaleFactor=1, mobile=False)
                chrome.send("Page.navigate", chrome.session, url=page.as_uri())
                while chrome.run("document.readyState") != "complete":
                    time.sleep(0.05)
                time.sleep(0.5)
                shot = chrome.send("Page.captureScreenshot", chrome.session, format="png")
                out = ROOT / "docs" / "screenshots" / f"issue-482-tickets-{width}.png"
                out.write_bytes(base64.b64decode(shot["data"]))
                print(out)
                print(width, json.loads(chrome.run(MEASURE)))
        finally:
            chrome.close()


if __name__ == "__main__":
    main()
