import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_dashboard_lanes import Browser, _page_with_status_stub, _rail_lanes  # noqa: E402

PROBE = """(function () {
  return [].map.call(document.querySelectorAll('#lanes .lane[data-device]:not(#queue)'), function (lane) {
    var rail = lane.querySelector('.rail');
    return {device: lane.getAttribute('data-device'), height: lane.getBoundingClientRect().height,
            tiles: lane.querySelectorAll('.tiles > .tile').length, rail: rail ? rail.textContent : null};
  }).concat([{scrollWidth: document.documentElement.scrollWidth}]);
})()"""


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        snapshot = _rail_lanes(Path(tmp))
        snapshot["queued"] = [dict(snapshot["queued"][0], job=f"q-{i}", no_waiter=None, no_waiter_link=None)
                              for i in range(2)]
        html = _page_with_status_stub(snapshot, "index.html")
    chrome = Browser()
    try:
        for width in (1440, 375):
            chrome.open(html, width)
            print(width, json.dumps(chrome.eval(PROBE)))
            out = ROOT / "docs" / "screenshots" / f"issue-644-lanes-{width}.png"
            chrome.screenshot(out, width)
            print(out)
    finally:
        chrome.close()


if __name__ == "__main__":
    main()
