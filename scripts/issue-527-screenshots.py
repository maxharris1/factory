import base64
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests.test_dashboard_system as system  # noqa: E402
from lib.dashboard import render_pages  # noqa: E402
from tests.test_dashboard_lanes import _page_with_status_stub  # noqa: E402
from tests.test_dashboard_layout import _snapshot  # noqa: E402

SHOTS = {
    "lanes": "index.html",
    "lanes-stale": "index.html",
    "tickets": "job.html",
    "activity": "activity.html",
    "efficiency": "efficiency.html",
    "system": "health.html",
}


def main() -> None:
    if len(sys.argv) > 1:
        system.CHROME = sys.argv[1]
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = _snapshot(Path(tmp))
        pages = render_pages(snapshot)
        stale = dict(snapshot, generated_at_epoch=time.time() - 554)
        stale["health"] = dict(stale["health"], publish_seconds=15)
        chrome = system._Chrome(Path(tmp) / "profile")
        try:
            for name, file in SHOTS.items():
                page = Path(tmp) / f"{name}.html"
                html = _page_with_status_stub(stale, file) if name == "lanes-stale" else pages[file]
                page.write_text(html, encoding="utf-8")
                for width, height in ((1440, 1600), (375, 2600)):
                    chrome.send("Emulation.setDeviceMetricsOverride", chrome.session,
                                width=width, height=height, deviceScaleFactor=1, mobile=False)
                    chrome.send("Page.navigate", chrome.session, url=page.as_uri())
                    while chrome.run("document.readyState") != "complete":
                        time.sleep(0.05)
                    time.sleep(2.5 if name == "lanes-stale" else 0.5)
                    shot = chrome.send("Page.captureScreenshot", chrome.session, format="png")
                    out = ROOT / "docs" / "screenshots" / f"issue-527-{name}-{width}.png"
                    out.write_bytes(base64.b64decode(shot["data"]))
                    print(out)
        finally:
            chrome.close()


if __name__ == "__main__":
    main()
