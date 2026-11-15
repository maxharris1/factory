import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.dashboard import render_pages  # noqa: E402
from tests.test_dashboard_activity import _rich, activity_snapshot  # noqa: E402

CHROME = sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / ".cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "activity.html"
        page.write_text(render_pages(_rich(activity_snapshot(Path(tmp))))["activity.html"], encoding="utf-8")
        for width, height in ((1440, 1000), (375, 1400)):
            out = ROOT / "docs" / "screenshots" / f"issue-143-activity-{width}.png"
            subprocess.run(
                [CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
                 f"--window-size={width},{height}", f"--screenshot={out}", page.as_uri()],
                check=True, capture_output=True, timeout=120,
            )
            print(out)


if __name__ == "__main__":
    main()
