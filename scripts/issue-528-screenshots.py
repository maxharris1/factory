import base64
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests.test_dashboard_system as system  # noqa: E402
from tests.test_dashboard_lanes import _counted, _page_with_status_stub  # noqa: E402

PROBE = """JSON.stringify((() => {
  const h2 = document.querySelector('#lanes h2');
  const title = h2.children[0].getBoundingClientRect();
  const count = h2.querySelector('.count');
  const held = document.querySelector('#lanes .px-held');
  return {
    kpis: !!document.getElementById('kpis'),
    count: count.textContent,
    classes: Array.from(count.querySelectorAll('b')).map(b => b.className + ':' + getComputedStyle(b).color),
    countBelowTitle: count.getBoundingClientRect().top >= title.bottom,
    heldAnimation: getComputedStyle(held, '::after').animationName,
    scrollWidth: document.documentElement.scrollWidth,
  };
})())"""


def main() -> None:
    if len(sys.argv) > 1:
        system.CHROME = sys.argv[1]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        page = Path(tmp) / "index.html"
        page.write_text(_page_with_status_stub(_counted(Path(tmp)), "index.html"), encoding="utf-8")
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
                out = ROOT / "docs" / "screenshots" / f"issue-528-lanes-{width}.png"
                out.write_bytes(base64.b64decode(shot["data"]))
                print(out)
        finally:
            chrome.close()


if __name__ == "__main__":
    main()
