import base64
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests.test_dashboard_system as system  # noqa: E402
from lib.dashboard import render_pages  # noqa: E402
from tests.test_dashboard_activity import _long, activity_snapshot  # noqa: E402
from tests.test_dashboard_lanes import CHROME  # noqa: E402


def main() -> None:
    system.CHROME = sys.argv[1] if len(sys.argv) > 1 else CHROME
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "activity.html"
        page.write_text(render_pages(_long(activity_snapshot(Path(tmp))))["activity.html"], encoding="utf-8")
        chrome = system._Chrome(Path(tmp) / "profile")
        try:
            for width, height in ((1440, 1200), (375, 2400)):
                chrome.send("Emulation.setDeviceMetricsOverride", chrome.session,
                            width=width, height=height, deviceScaleFactor=1, mobile=False)
                chrome.send("Page.navigate", chrome.session, url=page.as_uri())
                while chrome.run("document.readyState") != "complete":
                    time.sleep(0.05)
                time.sleep(0.5)
                shot = chrome.send("Page.captureScreenshot", chrome.session, format="png")
                out = ROOT / "docs" / "screenshots" / f"issue-535-activity-{width}.png"
                out.write_bytes(base64.b64decode(shot["data"]))
                print(out)
        finally:
            chrome.close()


if __name__ == "__main__":
    main()
