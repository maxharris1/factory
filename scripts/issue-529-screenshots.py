import base64
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests.test_dashboard_system as system  # noqa: E402
from tests.test_dashboard_lanes import CHROME, _page_with_status_stub  # noqa: E402
from tests.test_dashboard_layout import _snapshot  # noqa: E402

OPEN_CHART = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const article = document.getElementById('job-h-review');
  const chart = article.querySelector('details.jd-chart');
  chart.open = true;
  await sleep(400);
  const canvas = chart.querySelector('canvas');
  const ctx = canvas.getContext('2d');
  const font = "'JetBrains Mono', ui-monospace, Menlo, monospace";
  const width = (size, text) => { ctx.font = '400 ' + size + 'px ' + font; return ctx.measureText(text).width; };
  chart.scrollIntoView();
  return {
    canvas: [canvas.width, canvas.height],
    gloss: {'approved, waiting to merge': width(8, 'approved, waiting to merge'), 'merging base in': width(9, 'merging base in')},
    sideways: document.documentElement.scrollWidth - innerWidth,
  };
})()
"""


def main() -> None:
    system.CHROME = sys.argv[1] if len(sys.argv) > 1 else CHROME
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        page = Path(tmp) / "job.html"
        page.write_text(_page_with_status_stub(_snapshot(Path(tmp)), "job.html"), encoding="utf-8")
        chrome = system._Chrome(Path(tmp) / "profile")
        try:
            chrome.send("Page.navigate", chrome.session, url=page.as_uri() + "#job-h-review")
            chrome.wait_for("/ live/.test(' ' + document.getElementById('link').className)", "the live page")
            for width in (1440, 375):
                chrome.send("Emulation.setDeviceMetricsOverride", chrome.session,
                            width=width, height=900, deviceScaleFactor=1, mobile=False)
                print(width, json.dumps(chrome.run(OPEN_CHART)))
                height = chrome.run("document.documentElement.scrollHeight")
                chrome.send("Emulation.setDeviceMetricsOverride", chrome.session,
                            width=width, height=height, deviceScaleFactor=1, mobile=False)
                time.sleep(0.5)
                shot = chrome.send("Page.captureScreenshot", chrome.session, format="png")
                out = ROOT / "docs" / "screenshots" / f"issue-529-job-{width}.png"
                out.write_bytes(base64.b64decode(shot["data"]))
                print(out)
        finally:
            chrome.close()


if __name__ == "__main__":
    main()
