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
    "CSS": "4aaeeeb1c6c55f69fa5cbef9b078045f32a049bfa3b8ebb7bb12d123433e2264",
    "SCRIPT": "9f224372eacd16f2f0fa473c5050940635a3d0106cd682e4526cb2689a0a3c9d",
    "PX_SCRIPT": "b9353d15709b8b09e1de473712f8b909633ebe0cdfda2abcd987acf9b62a92b4",
    "page_script": "64141a1da6fd3c073bcb0924ca8853702ca11919dfe030bf247b7024a67d32fd",
    "seeded/index.html": "a2e0a53ad0b4cf0808427655fbb69540cb3a414e3bd944a81a547a7237803eaa",
    "seeded/job.html": "6e1ff22246f041cd4019f3f6932570a89f0a1e4a28fd6586291d7d62ecd1a948",
    "seeded/activity.html": "ff3c429ed0645cd79c4a5b2a942643267da268011683c2d45685a29d5289f73f",
    "seeded/efficiency.html": "f69bf33c5f4e4b39572e8d760d2e7a4baecb08f418eef5af34b905badf3ee78c",
    "seeded/health.html": "785710df15b370be52f02c5c0b57256495d74956f3eed99b26b86047158a3330",
    "hostile/index.html": "83f45962454658d549f34ea8eaefc44b3e95bdc7d57a6dc80dacae88af91331d",
    "hostile/job.html": "89da1d981ff8619b4fb37a63dde44ee10adc2a64c09033416ce678aac50c9136",
    "hostile/activity.html": "76d7cf2f6f046818a08be0f2691232619069862491a6e56190dcdde93b41de5d",
    "hostile/efficiency.html": "59266798eece4fb51e9d6785a348f12814142166a9d49fe8ddc922c8a5c33d61",
    "hostile/health.html": "fa4d7c0e1fe4ecf798d1bbf411eda65de771da8aee5f05a62dffe1f88530ab16",
    "empty/index.html": "05d2adf3769dbd583a0f5b341d0c92424d4673f34f452604f33194a639580d99",
    "empty/job.html": "3d8a7af0c9fabe900371dbf6479a3a74c703435ff75e92e7c3e8c16372b50d9a",
    "empty/activity.html": "fd846a43835a460746d8a969078612e6a8e25be7920fe7ef1d980f098f76a8fe",
    "empty/efficiency.html": "7431f559cc9a29f0b6aa8ac7bb39703f2dd100aaf97468dc075b072b77f38bf6",
    "empty/health.html": "79b3614e092aa0591fbc360049c80719aa06c5d08cbf37e628c9f01c543a10c8",
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
    "_ATTENTION", "_HEALTH_LINK", "_health_link", "_attention", "_HANDOFF", "_handoffs",
    "_VERDICT", "_verdicts", "_td", "_tickets", "_log", "_POLL_STATE", "_BUDGET_STATE",
    "_health", "_HEALTH_STATE", "_ANCHOR", "_anchor_attr", "_components", "_no_waiter",
    "_savings", "_active_jobs", "_board", "_legend", "_TRAIL_LEGEND", "_meta", "_job_article",
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
