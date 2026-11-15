"""Render the #149 screenshots of job.html#job-h-review with a live telemetry
row (tests/test_dashboard_live_run.LIVE_ROW: a plan of 6 with 2 done and 1 in
progress, one running sub-agent) at 1440 and 375 px wide, full page, through
headless Chromium over CDP.

    python3 -m tests.render_live_run_screenshots [path/to/chrome]

Needs node 22 (global WebSocket) and a Chromium binary: the argument, $CHROME,
or Playwright's cached one.
"""

from __future__ import annotations

import functools
import http.server
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from lib.dashboard import write_snapshot_pages
from tests.render_board_screenshots import DRIVER, OUT, _chrome, _Quiet
from tests.test_dashboard import NOW
from tests.test_dashboard_live_run import LIVE_ROW, _snapshot

WIDTHS = (1440, 375)
LIVE_DRIVER = DRIVER.replace("'.html' }", "'.html#job-h-review' }").replace(
    "`${out}/issue-142-${page === 'job' ? 'tickets' : page}-${width}.png`",
    "`${out}/issue-149-ticket-detail-${width}.png`",
)


def main() -> None:
    assert LIVE_DRIVER.count("issue-149") == 1 and "#job-h-review" in LIVE_DRIVER
    with tempfile.TemporaryDirectory() as tmp:
        site = Path(tmp) / "site"
        site.mkdir()
        write_snapshot_pages(site, _snapshot(Path(tmp), LIVE_ROW))
        handler = functools.partial(_Quiet, directory=str(site))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            subprocess.run(
                ["node", "-e", LIVE_DRIVER, _chrome(), f"http://127.0.0.1:{server.server_port}", str(OUT),
                 str(int(NOW * 1000)), *(f"job:{width}" for width in WIDTHS)],
                check=True, timeout=180,
            )
        finally:
            server.shutdown()
    for width in WIDTHS:
        sys.stdout.write(f"{OUT / f'issue-149-ticket-detail-{width}.png'}\n")


if __name__ == "__main__":
    main()
