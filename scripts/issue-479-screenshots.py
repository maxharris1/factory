import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.dashboard import render_pages  # noqa: E402
from tests.test_dashboard_layout import _snapshot  # noqa: E402

CHROME = sys.argv[1] if len(sys.argv) > 1 else "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SHOTS = {
    "lanes": ("index.html", ""),
    "tickets": ("job.html", ""),
    "ticket-detail": ("job.html", "#job-h-review"),
    "activity": ("activity.html", ""),
    "efficiency": ("efficiency.html", ""),
    "system": ("health.html", ""),
}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        pages = render_pages(_snapshot(Path(tmp)))
        for name, (file, hash_) in SHOTS.items():
            page = Path(tmp) / file
            page.write_text(pages[file], encoding="utf-8")
            for width, height in ((1440, 1600), (375, 2600)):
                out = ROOT / "docs" / "screenshots" / f"issue-479-{name}-{width}.png"
                subprocess.run(
                    [CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
                     f"--window-size={width},{height}", f"--screenshot={out}", page.as_uri() + hash_],
                    check=True, capture_output=True, timeout=120,
                )
                print(out)


if __name__ == "__main__":
    main()
