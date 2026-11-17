import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_dashboard_lanes import Browser, _idle_lanes, _page_with_status_stub  # noqa: E402

PROBE = """(function () {
  return [].map.call(document.querySelectorAll('#lanes .lane[data-device]:not(#queue)'), function (lane) {
    return {device: lane.getAttribute('data-device'), height: lane.getBoundingClientRect().height,
            tiles: [].map.call(lane.querySelectorAll('.tiles > .tile'), function (t) {
              var b = t.getBoundingClientRect(), lbl = t.querySelector('.lbl');
              return [lbl ? lbl.textContent : 'job', Math.round(b.width), Math.round(b.height)]; })};
  }).concat([{scrollWidth: document.documentElement.scrollWidth}]);
})()"""


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        snapshot = _idle_lanes(Path(tmp))
        snapshot["queued"] = [dict(snapshot["queued"][0], job=f"q-{i}", no_waiter=None, no_waiter_link=None)
                              for i in range(2)]
        html = _page_with_status_stub(snapshot, "index.html")
    chrome = Browser()
    try:
        for width in (1440, 375):
            chrome.open(html, width)
            print(width, json.dumps(chrome.eval(PROBE)))
            out = ROOT / "docs" / "screenshots" / f"issue-718-lanes-{width}.png"
            chrome.screenshot(out, width)
            print(out)
    finally:
        chrome.close()


if __name__ == "__main__":
    main()
