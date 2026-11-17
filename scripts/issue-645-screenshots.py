import base64
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests.test_dashboard_system as system  # noqa: E402
from tests.test_dashboard_lanes import _page_with_status_stub, _tiles  # noqa: E402

PROBE = """JSON.stringify((() => {
  const intake = document.getElementById('intake');
  return {
    anchors: ['intake', 'feeder', 'linear-webhook'].map(id => {
      const node = document.getElementById(id);
      return id + ':' + (node ? node.getBoundingClientRect().height > 0 : 'missing');
    }),
    count: intake.querySelector('h2 .count').textContent,
    order: Array.from(intake.querySelectorAll('.irow, .igroup, .ticket, summary'))
      .map(n => n.id || n.textContent),
    closed: Array.from(intake.querySelectorAll('details')).map(d => d.open),
    scrollWidth: document.documentElement.scrollWidth,
  };
})())"""


def main() -> None:
    if len(sys.argv) > 1:
        system.CHROME = sys.argv[1]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        page = Path(tmp) / "index.html"
        page.write_text(_page_with_status_stub(_tiles(Path(tmp)), "index.html"), encoding="utf-8")
        chrome = system._Chrome(Path(tmp) / "profile")
        try:
            chrome.send("Page.navigate", chrome.session, url=page.as_uri())
            chrome.wait_for("/ live/.test(' ' + document.getElementById('link').className)", "the live page")
            for width in (1440, 375):
                chrome.send("Emulation.setDeviceMetricsOverride", chrome.session,
                            width=width, height=900, deviceScaleFactor=1, mobile=False)
                time.sleep(0.5)
                print(width, json.loads(chrome.run(PROBE)))
                height = chrome.run("document.documentElement.scrollHeight")
                chrome.send("Emulation.setDeviceMetricsOverride", chrome.session,
                            width=width, height=height, deviceScaleFactor=1, mobile=False)
                time.sleep(0.5)
                shot = chrome.send("Page.captureScreenshot", chrome.session, format="png")
                out = ROOT / "docs" / "screenshots" / f"issue-645-lanes-{width}.png"
                out.write_bytes(base64.b64decode(shot["data"]))
                print(out)
        finally:
            chrome.close()


if __name__ == "__main__":
    main()
