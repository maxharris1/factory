import base64
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests.test_dashboard_system as system  # noqa: E402
from tests.test_dashboard_lanes import _page_with_status_stub  # noqa: E402


def main() -> None:
    if len(sys.argv) > 1:
        system.CHROME = sys.argv[1]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        page = Path(tmp) / "health.html"
        page.write_text(_page_with_status_stub(system._classified(Path(tmp)), "health.html"), encoding="utf-8")
        chrome = system._Chrome(Path(tmp) / "profile")
        try:
            chrome.send("Page.navigate", chrome.session, url=page.as_uri())
            chrome.wait_for("/ live/.test(' ' + document.getElementById('link').className)", "the live page")
            for width in (1440, 375):
                chrome.send("Emulation.setDeviceMetricsOverride", chrome.session,
                            width=width, height=900, deviceScaleFactor=1, mobile=False)
                height = chrome.run("document.documentElement.scrollHeight")
                chrome.send("Emulation.setDeviceMetricsOverride", chrome.session,
                            width=width, height=height, deviceScaleFactor=1, mobile=False)
                time.sleep(0.5)
                shot = chrome.send("Page.captureScreenshot", chrome.session, format="png")
                out = ROOT / "docs" / "screenshots" / f"issue-486-system-{width}.png"
                out.write_bytes(base64.b64decode(shot["data"]))
                print(out)
        finally:
            chrome.close()


if __name__ == "__main__":
    main()
