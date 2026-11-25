"""Static CSS and JS assets for Mission Control (E4-3).

Every dashboard stylesheet and script lives under `lib/dashboard_static/css/`
and `lib/dashboard_static/js/` as a real `.css` / `.js` file rather than a
multiline Python string literal. `write_snapshot_pages()` publishes the
assembled flat files (`STATIC_ASSETS`) atomically alongside `status.json`,
the HTML pages, and `tickets.json`, and `deploy/factory-dashboard-http.py`
serves them under `/static/<name>` with a strict `script-src 'self'` CSP.
"""

from __future__ import annotations

import functools
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent
STATIC_ASSETS = ("app.css", "app.js", "auth.js")
STATIC_CONTENT_TYPES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "auth.js": "application/javascript; charset=utf-8",
}


@functools.lru_cache(maxsize=None)
def read_asset(relpath: str) -> str:
    """Read a UTF-8 static asset with normalized LF newlines."""
    path = (STATIC_DIR / relpath).resolve()
    if STATIC_DIR.resolve() not in path.parents:
        raise ValueError(f"asset path escapes static root: {relpath}")
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def auth_script() -> str:
    """Sign-in screen script served at /static/auth.js (no inline script)."""
    return read_asset("js/auth.js")
