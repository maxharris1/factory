import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.dashboard import collect_snapshot, render_pages  # noqa: E402
from tests.test_dashboard import NOW, add_ticket, seeded  # noqa: E402
from tests.test_dashboard_efficiency import _machines, _readings  # noqa: E402

CHROME = sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / ".cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell")
SHIPPED = {
    0: ["cleffy#96", "cleffy#97", "factory#65", "factory#72", "factory#81"],
    1: ["cleffy#98", "factory#95", "factory#97", "factory#104"],
    2: ["factory#74", "factory#82"],
    3: ["factory#98"],
}


def snapshot(tmp: Path) -> dict:
    db, log = seeded(tmp)
    with sqlite3.connect(db) as connection:
        for rounds, refs in SHIPPED.items():
            for ref in refs:
                repo, ticket = ref.split("#")
                add_ticket(connection, f"maxharris1/{repo}", ticket, state="merged", review_round=rounds)
        _readings(connection)
    data = collect_snapshot(db, log, now=NOW, machines_path=_machines(tmp))
    data["handoff_savings"].update(scripted=115, scripted_in_flight=8, bot_to_bot=62,
                                   scripted_share=115 / 177)
    data["queue"]["totals"].update(done=174, failed=74)
    return data


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "efficiency.html"
        page.write_text(render_pages(snapshot(Path(tmp)))["efficiency.html"], encoding="utf-8")
        for width, height in ((1440, 1420), (375, 2500)):
            out = ROOT / "docs" / "screenshots" / f"issue-144-efficiency-{width}.png"
            subprocess.run(
                [CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
                 f"--window-size={width},{height}", f"--screenshot={out}", page.as_uri()],
                check=True, capture_output=True, timeout=120,
            )
            print(out)


if __name__ == "__main__":
    main()
