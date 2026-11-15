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
from tests.test_dashboard import NOW, make_db, seeded

FACADE = Path(__file__).resolve().parent.parent / "lib" / "dashboard_page.py"

GOLDEN = {
    "CSS": "4ea9da2b5074e1671a95b515684d2627827783f472cf60a34eff4ddaeee88a71",
    "SCRIPT": "9a5275ec47e22f8388d839b94cb82cf61a7943f737903d226efa6069b1e5ce80",
    "PX_SCRIPT": "d3c012a9dcc3b97338a731e172ea14cd5d25dfc59c7fab2fdd867bb974b9dc1b",
    "page_script": "812b58b5ee9b48ffde3a2bd44ee18bdc4880d155bc2bbd15132f66e3abe031ab",
    "seeded/index.html": "e3b3a0661d989314f01b3ae16d1da540e7ae9c5960a9c3c6691596bfdbb6aa56",
    "seeded/job.html": "261813f19600204a9b9031fed67931bf13f86181c027f0895791176d6232d241",
    "seeded/activity.html": "3b09313ac17b190fe36034e8938bf1bb85342ba152bc9b587ee022aeeea26502",
    "seeded/efficiency.html": "0bb5186363c19a2954560ce9aaabcf03f667f75eebc6be0b65525e061a2b6458",
    "seeded/health.html": "7ffc39c4ed50094eaba29ee288c8c3de3c09a60a7549a3b480dc1112e98017cc",
    "hostile/index.html": "634922ce4d42864f35bbc591d1813f52d7f2f8bcc21a5d21ff1392dc9cb75a5f",
    "hostile/job.html": "2b021c64c3c31dbe426e655f63dc8ee6e6425a9815ceca54bbfedcfb080624c7",
    "hostile/activity.html": "5108ef6b16b23c774a5a4705c42e6739809d02d47ad40c42f34b00d02abeaabc",
    "hostile/efficiency.html": "471f99156c0ef92e31c2cf0ff65fa205341bae50f222beca3ee1ea484bf66b7a",
    "hostile/health.html": "44d309df6e34d2c868edd8e1b2ac41c9d92a66f211a48114c84878ba77df6135",
    "empty/index.html": "4ed1348c9c0bf6dc0b1f1ce31767e456fb7e14e9e104a98771a748d2957e1fad",
    "empty/job.html": "efcce4d8143a5e0af86586be5ca52b8db0e10a58734520cf7c51e24f03a3a98b",
    "empty/activity.html": "2373648f14a54b6d9fbaaef24b1f660cf61e44b6e79cb1543fc30831d739dea9",
    "empty/efficiency.html": "5e132c8b5ddbe1a030433d8d266383959cb8461a7f3cb738ac4e7c90cf2a75bc",
    "empty/health.html": "9482923654539424f0c723077692c5f3947b148e83bfcc9a51f4c66bc103c306",
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
    "_linear_webhook", "_JOB_STATE", "_state_chip", "_slot", "lane_devices", "_lanes", "_queue",
    "_ATTENTION", "_HEALTH_LINK", "_health_link", "_attention", "_HANDOFF", "_handoffs",
    "_VERDICT", "_verdicts", "_td", "_tickets", "_log", "_POLL_STATE", "_BUDGET_STATE",
    "_health", "_HEALTH_STATE", "_ANCHOR", "_anchor_attr", "_components", "_no_waiter",
    "_savings", "_active_jobs", "_jobs", "_legend", "_TRAIL_LEGEND", "_meta", "_job_article",
    "_jobview", "_flow_template", "_PANELS", "_page", "render_pages",
    "path_geometry", "render_path_svg", "render_svg", "TELEMETRY_CSS", "TELEMETRY_SCRIPT",
    "job_block", "rollup_panel", "CODE_COMPONENTS", "label",
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
        "page_script": _sha(page.page_script()),
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
