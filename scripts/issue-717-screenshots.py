import base64
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_dashboard_ci import CI_LAYOUT, SHOTS, ci_snapshot  # noqa: E402
from tests.test_dashboard_lanes import Browser, _page_with_status_stub  # noqa: E402

SECTION = """(function () {
  var b = document.getElementById('ci-runs').getBoundingClientRect();
  return {x: Math.max(0, b.left - 12), y: Math.max(0, b.top + window.scrollY - 12),
          width: Math.min(document.documentElement.clientWidth, b.width + 24), height: b.height + 24};
})()"""


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        html = _page_with_status_stub(ci_snapshot(Path(tmp)), "index.html")
    chrome = Browser()
    try:
        for name, width in zip(SHOTS, (1440, 375)):
            chrome.open(html, width)
            height = chrome.eval("document.documentElement.scrollHeight")
            chrome.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=2,
                        mobile=False)
            print(width, json.dumps(chrome.eval(CI_LAYOUT)))
            clip = dict(chrome.eval(SECTION), scale=1)
            out = ROOT / "docs" / "screenshots" / name
            out.write_bytes(base64.b64decode(chrome.call("Page.captureScreenshot", format="png", clip=clip)["data"]))
            print(out)
    finally:
        chrome.close()


if __name__ == "__main__":
    main()
