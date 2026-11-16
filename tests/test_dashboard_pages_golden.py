"""#271: lib/dashboard_page.py split into lib/dashboard_pages/ moved no byte.

The digests were recorded from the single-file module on main (595745d).
A change that alters the served pages on purpose records them again with
`python -m tests.test_dashboard_pages_golden`.
"""

from __future__ import annotations

import copy
import hashlib
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

import lib.dashboard_page as page
import lib.health
from lib.dashboard import collect_snapshot
from lib.dashboard_flow import load_chart
from tests.test_dashboard import NOW, make_db, seeded

FACADE = Path(__file__).resolve().parent.parent / "lib" / "dashboard_page.py"

GOLDEN = {
    "CSS": "1c8859ef0f4437f798f3a2f1091f4a81881ef8e2429d44d6a418aa6316eda736",
    "SCRIPT": "7131dd2f38b1211bc6f6a38fc8fa4ad798012cc0149f225ba4553bb90674fac0",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "3c1ae2d48bc5db32976b4267ebf57c8e4e97ff2cbf1f6aae96596aa19407a7a4",
    "seeded/index.html": "21f8c543bba2be487040c788e1f5979f14b887e117642a701369bb9346e61475",
    "seeded/job.html": "fa01a9bc0d1943331e73877ac8b2be4ab0e67239d8192dfeb6838d61917c77d6",
    "seeded/activity.html": "3c249f2d06130e75f26a951086105fefd67f24cc5f4a32aa67be02edfaf9b10c",
    "seeded/efficiency.html": "788a1c54a90db20a097098d44585c94506f058ce08daaae1726dd4abc937b885",
    "seeded/health.html": "e1d490a188d285de79868397e28323f80becb335fcc770580f0ee45d1b481085",
    "hostile/index.html": "baf88c7ff03b5e57d3c9b1e189450bd56256dcd4cbe536cc183bc36bfe61095a",
    "hostile/job.html": "6d26217b208731dcd811eacc9f3f69fb722bc1385c818b187f14a09ae2f7cdae",
    "hostile/activity.html": "1ea652965c774cca9e9b0c5a8f2ed65c5af7d318c9adcb930c3aec57bf6e678d",
    "hostile/efficiency.html": "412aa0837f67a8623e21508cd4da100e793a37d91b0e89a524db2c487b99fbf8",
    "hostile/health.html": "c427c67a0d24be34581c8be33ec95e81cd96d656297cea22fe65dc3ccdf01d34",
    "empty/index.html": "c767bf9edba9ee548c82046768085376e5218139d2d6244c0198cc8b71132129",
    "empty/job.html": "5a7634accc93a6f4fe8377de7e7c98975fdb28faf132bae77bf7d8abd2ccb420",
    "empty/activity.html": "b8e3f8d4aed6c77541e0a3c650ac69326f3901ac518847996295591eca29a20f",
    "empty/efficiency.html": "e80a7a21e7a54bd8baa33c4dc439bc392541857278c036de08cb2edf24665b8f",
    "empty/health.html": "a92e50552f9d1de7ca3f132fdf53da43f8c9d343f80d2d37787fd89a5bc079db",
}

NAMES = (
    "POLL_MS", "FONT_DIR", "FONTS", "CSS", "SCRIPT", "PX_SCRIPT", "font_faces", "px_script",
    "page_script", "PAGES", "_PAGE_NAMES", "LIST_MAX", "_BOTS", "_e", "PX_EFFECTS", "PX_ONESHOTS",
    "PX_BOT_TINTS", "PX_FALLBACK", "_PX_BAD", "PX_BROKEN", "_PX_LINK", "PX_STATE", "PX_ACTIONS",
    "_PX_HEX", "px_table", "px_state", "_px_progress", "px_frame", "_fixed3", "px_strip",
    "px_job_strip", "duration", "_num", "_short", "_ticket", "_job_label", "_job_kind", "anchor",
    "_b", "_span", "_bot", "_chip", "_panel", "_feed", "_when", "_topbar", "header_load", "_band",
    "_attention_strip", "STATUS_LINKS", "STATUS_ABSENT_LINK", "shell_table", "version_drift",
    "_segment", "_count", "status_segments", "_statusline", "_kpis", "_FEEDER_STATE", "_feeder",
    "_linear_webhook", "_JOB_STATE", "_state_chip", "_slot", "lane_devices", "_lanes", "_queue_lane", "_just_happened", "_intake",
    "_ATTENTION", "_HEALTH_LINK", "_health_link", "_attention", "_relay", "relay_threads",
    "_VERDICT", "_td", "_tickets", "_log", "_POLL_STATE", "_BUDGET_STATE",
    "_health", "_HEALTH_STATE", "_ANCHOR", "_anchor_attr", "_components", "_no_waiter",
    "_savings", "_active_jobs", "_board", "_detail_model", "_job_article",
    "_jobview", "_PANELS", "_page", "render_pages", "TELEMETRY_CSS", "TELEMETRY_SCRIPT",
    "rollup_panel", "CODE_COMPONENTS", "label",
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snapshots(root: Path) -> dict[str, dict]:
    (root / "seeded").mkdir()
    db, log = seeded(root / "seeded")
    os.utime(log, (NOW - 30, NOW - 30))
    seeded_snapshot = collect_snapshot(db, log, now=NOW)
    hostile = copy.deepcopy(seeded_snapshot)
    hostile["attention"] = {"count": 2, "items": [
        {"kind": "down", "repo": "", "ticket_id": "", "component": "<b>x</b>",
         "link": "javascript:alert(1)", "detail": "down \"now\""},
        {"kind": "stuck", "repo": "</script><script>x()</script>", "ticket_id": "'1'",
         "detail": "<img src=x onerror=y>"}]}
    empty = root / "empty"
    empty.mkdir()
    make_db(empty / "orch.db")
    (empty / "orch.log").write_text("")
    os.utime(empty / "orch.log", (NOW - 30, NOW - 30))
    return {
        "seeded": seeded_snapshot,
        "hostile": hostile,
        "empty": collect_snapshot(empty / "orch.db", empty / "orch.log", now=NOW),
    }


def digests(root: Path) -> dict[str, str]:
    with mock.patch.object(lib.health, "factory_version", lambda root=None: "0123456789ab"):
        return _digests(root)


def _digests(root: Path) -> dict[str, str]:
    found = {
        "CSS": _sha(page.CSS),
        "SCRIPT": _sha(page.SCRIPT),
        "PX_SCRIPT": _sha(page.PX_SCRIPT),
        "page_script": _sha(page.page_script(load_chart())),
    }
    for case, snapshot in _snapshots(root).items():
        for name, text in page.render_pages(snapshot).items():
            found[f"{case}/{name}"] = _sha(text)
    return found


def test_every_page_and_asset_is_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


def test_every_page_is_covered(tmp_path: Path) -> None:
    assert {key.split("/")[1] for key in GOLDEN if "/" in key} == set(page.PAGES)


@pytest.mark.parametrize("name", NAMES)
def test_the_facade_still_exports(name: str) -> None:
    assert hasattr(page, name)


def test_the_facade_is_under_150_lines() -> None:
    assert len(FACADE.read_text().splitlines()) < 150


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        for key, value in digests(Path(tmp)).items():
            sys.stdout.write(f'    "{key}": "{value}",\n')
