"""Render the #146 screenshots of ticket.html#t=maxharris1%2Fcleffy%2340 (ticket 40 from
tests/test_dashboard_layout._history) at 1440 and 375 px wide, full page,
through headless Chromium over CDP.

    python3 -m tests.render_ticket_detail_screenshots [path/to/chrome]

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
from lib.dashboard_pages.common import ticket_page_href
from tests.render_board_screenshots import DRIVER, OUT, _chrome, _Quiet
from tests.test_dashboard import NOW
from tests.test_dashboard_layout import _snapshot

WIDTHS = (1440, 375)
HASH = ticket_page_href({"repo": "maxharris1/cleffy", "ticket_id": "40"}).removeprefix("ticket.html")
TICKET_DRIVER = DRIVER.replace("'.html' }", f"'.html{HASH}' }}").replace(
    "`${out}/issue-142-${page === 'job' ? 'tickets' : page}-${width}.png`",
    "`${out}/issue-146-ticket-detail-${width}.png`",
)


def main() -> None:
    assert TICKET_DRIVER.count("issue-146") == 1 and HASH in TICKET_DRIVER
    with tempfile.TemporaryDirectory() as tmp:
        site = Path(tmp) / "site"
        site.mkdir()
        write_snapshot_pages(site, _snapshot(Path(tmp)))
        handler = functools.partial(_Quiet, directory=str(site))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            subprocess.run(
                ["node", "-e", TICKET_DRIVER, _chrome(), f"http://127.0.0.1:{server.server_port}", str(OUT),
                 str(int(NOW * 1000)), *(f"ticket:{width}" for width in WIDTHS)],
                check=True, timeout=180,
            )
        finally:
            server.shutdown()
    for width in WIDTHS:
        sys.stdout.write(f"{OUT / f'issue-146-ticket-detail-{width}.png'}\n")


if __name__ == "__main__":
    main()
