import base64
import functools
import http.server
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests.test_dashboard_system as system  # noqa: E402
from lib.dashboard import render_pages  # noqa: E402
from tests.test_dashboard_tickets_board import board_snapshot  # noqa: E402

MEASURE = (
    "(() => { const box = document.querySelector('#ticket-filter .tk-find');"
    " const field = document.getElementById('find-ticket'); const list = document.getElementById('find-ticket-list');"
    " const tools = document.querySelector('#ticket-filter .tk-tools').getBoundingClientRect();"
    " const find = box.getBoundingClientRect(); const listed = list.getBoundingClientRect();"
    " return JSON.stringify({viewport: innerWidth, page: document.documentElement.scrollWidth,"
    " fits: document.documentElement.scrollWidth <= innerWidth,"
    " label: box.querySelector('label').textContent, labelFor: box.querySelector('label').htmlFor,"
    " focused: document.activeElement === field, value: field.value, caret: field.selectionStart,"
    " expanded: field.getAttribute('aria-expanded'), active: field.getAttribute('aria-activedescendant'),"
    " options: [...list.querySelectorAll('[role=option]')].map(o => [o.textContent, o.getAttribute('aria-selected')]),"
    " open: document.querySelector('.tk-open').getAttribute('href'),"
    " sameRow: Math.abs(find.top - tools.top) < 4, listInside: listed.right <= innerWidth && listed.left >= 0,"
    " height: Math.ceil(Math.max(listed.bottom, document.getElementById('pipeline').getBoundingClientRect().top)"
    " + scrollY) + 16}); })()"
)


def key(chrome: system._Chrome, name: str, code: int) -> None:
    for kind in ("keyDown", "keyUp"):
        chrome.send("Input.dispatchKeyEvent", chrome.session, type=kind, key=name, code=name,
                    windowsVirtualKeyCode=code)


def main() -> None:
    if len(sys.argv) > 1:
        system.CHROME = sys.argv[1]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        site = Path(tmp) / "site"
        site.mkdir()
        snapshot = board_snapshot(Path(tmp))
        for name, html in render_pages(snapshot).items():
            (site / name).write_text(html, encoding="utf-8")
        (site / "status.json").write_text(json.dumps(snapshot), encoding="utf-8")
        handler = functools.partial(system.QuietHandler, directory=str(site))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        chrome = system._Chrome(Path(tmp) / "profile")
        try:
            for width in (1440, 375):
                chrome.send("Emulation.setDeviceMetricsOverride", chrome.session,
                            width=width, height=900, deviceScaleFactor=1, mobile=False)
                chrome.send("Page.navigate", chrome.session, url=f"http://127.0.0.1:{server.server_address[1]}/job.html")
                chrome.wait_for("(document.getElementById('link-text') || {}).textContent === 'LIVE'", "the script's first render")
                chrome.run("document.getElementById('find-ticket').focus()")
                chrome.send("Input.insertText", chrome.session, text="1")
                key(chrome, "ArrowDown", 40)
                key(chrome, "ArrowDown", 40)
                chrome.run("window.dispatchEvent(new HashChangeEvent('hashchange'))")
                time.sleep(0.5)
                found = json.loads(chrome.run(MEASURE))
                print(width, found)
                assert found["label"] == "Find a ticket" and found["labelFor"] == "find-ticket"
                assert found["focused"] and found["value"] == "1" and found["caret"] == 1, "a redraw keeps the field"
                assert found["expanded"] == "true" and len(found["options"]) == 8, "eight matches listed"
                assert found["active"] == "find-ticket-1" and found["options"][1][1] == "true", "the pick survives"
                assert found["listInside"], "the list stays inside the viewport"
                assert found["fits"], "nothing scrolls sideways"
                if width == 1440:
                    assert found["sameRow"], "the search sits on the toolbar row"
                chrome.send("Emulation.setDeviceMetricsOverride", chrome.session,
                            width=width, height=found["height"], deviceScaleFactor=1, mobile=False)
                time.sleep(0.5)
                clip = {"x": 0, "y": 0, "width": width, "height": found["height"], "scale": 1}
                shot = chrome.send("Page.captureScreenshot", chrome.session, format="png", clip=clip)
                out = ROOT / "docs" / "screenshots" / f"issue-672-tickets-{width}.png"
                out.write_bytes(base64.b64decode(shot["data"]))
                print(out)
                key(chrome, "Enter", 13)
                chrome.wait_for("location.pathname.endsWith('/ticket.html')", "ticket page")
                print(width, "enter opened", chrome.run("location.pathname + location.hash"))
        finally:
            chrome.close()
            server.shutdown()


if __name__ == "__main__":
    main()
