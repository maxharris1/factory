"""Render the #142 screenshots of job.html and activity.html from the board
fixture snapshot (tests/test_dashboard_tickets_board.board_snapshot), at 1440
and 375 px wide, full page, through headless Chromium over CDP.

    python3 -m tests.render_board_screenshots [path/to/chrome]

Needs node 22 (global WebSocket) and a Chromium binary: the argument, $CHROME,
or Playwright's cached one.
"""

from __future__ import annotations

import functools
import glob
import http.server
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from lib.dashboard import write_snapshot_pages
from tests.test_dashboard import NOW
from tests.test_dashboard_tickets_board import board_snapshot

OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
NAMES = {"job": "tickets", "activity": "activity"}
SHOTS = [(page, width) for page in NAMES for width in (1440, 375)]

DRIVER = r"""
const [chrome, base, out, nowMs, ...shots] = process.argv.slice(1);
const { spawn } = require('child_process');
const fs = require('fs');
const proc = spawn(chrome, ['--headless=new', '--remote-debugging-port=0', '--no-sandbox',
  '--hide-scrollbars', '--user-data-dir=' + fs.mkdtempSync('/tmp/chrome-142-'), 'about:blank']);
let ws, id = 0;
const pending = new Map();
function send(method, params = {}, sessionId) {
  return new Promise((resolve, reject) => {
    const msg = { id: ++id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    pending.set(msg.id, { resolve, reject });
    ws.send(JSON.stringify(msg));
  });
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
proc.stderr.on('data', async (chunk) => {
  const m = /DevTools listening on (ws:\S+)/.exec(String(chunk));
  if (!m || ws) return;
  ws = new WebSocket(m[1]);
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    const p = pending.get(msg.id);
    if (!p) return;
    pending.delete(msg.id);
    msg.error ? p.reject(new Error(JSON.stringify(msg.error))) : p.resolve(msg.result);
  };
  ws.onopen = async () => {
    try {
      for (const shot of shots) {
        const [page, width] = shot.split(':');
        const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
        const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
        await send('Page.enable', {}, sessionId);
        await send('Emulation.setDeviceMetricsOverride',
          { width: Number(width), height: 900, deviceScaleFactor: 1, mobile: false }, sessionId);
        await send('Page.addScriptToEvaluateOnNewDocument', { source:
          `(function(){var shift=${nowMs}-Date.now();var D=Date;var now=D.now;` +
          `Date.now=function(){return now.call(D)+shift;};})();` }, sessionId);
        await send('Page.navigate', { url: base + '/' + page + '.html' }, sessionId);
        await sleep(3000);
        const { result } = await send('Runtime.evaluate',
          { expression: 'document.documentElement.scrollHeight', returnByValue: true }, sessionId);
        const height = Math.ceil(result.value);
        await send('Emulation.setDeviceMetricsOverride',
          { width: Number(width), height, deviceScaleFactor: 1, mobile: false }, sessionId);
        await sleep(800);
        const { data } = await send('Page.captureScreenshot',
          { format: 'png', clip: { x: 0, y: 0, width: Number(width), height, scale: 1 } }, sessionId);
        fs.writeFileSync(`${out}/issue-142-${page === 'job' ? 'tickets' : page}-${width}.png`,
          Buffer.from(data, 'base64'));
        await send('Target.closeTarget', { targetId });
      }
    } catch (err) { console.error(err); process.exitCode = 1; }
    proc.kill();
    process.exit();
  };
});
"""


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


def _chrome() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    found = sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")))
    if not found:
        sys.exit("no Chromium: pass its path or set CHROME")
    return found[-1]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        site = Path(tmp) / "site"
        site.mkdir()
        write_snapshot_pages(site, board_snapshot(Path(tmp)))
        handler = functools.partial(_Quiet, directory=str(site))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            subprocess.run(
                ["node", "-e", DRIVER, _chrome(), f"http://127.0.0.1:{server.server_port}", str(OUT),
                 str(int(NOW * 1000)), *(f"{page}:{width}" for page, width in SHOTS)],
                check=True, timeout=180,
            )
        finally:
            server.shutdown()
    for page, width in SHOTS:
        sys.stdout.write(f"{OUT / f'issue-142-{NAMES[page]}-{width}.png'}\n")


if __name__ == "__main__":
    main()
