import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_dashboard_activity import _endings, _job, _long, activity_snapshot  # noqa: E402
from tests.test_dashboard_lanes import Browser, _page_with_status_stub  # noqa: E402

SHOWN = {"131", "124", "201", "202", "203", "204", "205", "206", "208", "209", "210"}


def snapshot(tmp: Path) -> dict:
    base = activity_snapshot(tmp)
    long = _long(base)
    snap = _endings(base)
    threads = [*long["relay"]["threads"], *snap["relay"]["threads"]]
    snap["relay"]["threads"] = [t for t in threads if t["ticket_id"] in SHOWN]
    snap["relay"]["tracked"] = 40
    snap["running"].append(_job("run-124", "124", "fix", job_state="running", elapsed_seconds=665,
                                heartbeat_age_seconds=4))
    return snap


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        html = _page_with_status_stub(snapshot(Path(tmp)), "activity.html")
        browser = Browser()
        try:
            for width in (1440, 375):
                browser.open(html, width)
                if width == 1440:
                    browser.eval("document.querySelector('[data-more=\"maxharris1/factory#131\"]').click()")
                out = ROOT / "docs" / "screenshots" / f"issue-674-activity-{width}.png"
                browser.screenshot(out, width)
                print(out)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
