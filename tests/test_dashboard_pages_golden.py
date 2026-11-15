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
    "CSS": "e0a11b5c1fc3e332c1bcf46e441db33291aa1e52b18032ff49ce6e581d08127f",
    "SCRIPT": "3f8bd2997a685f92b5ff0629f33024b32c2392b40cd005f3414c83a5afcab545",
    "PX_SCRIPT": "c468396e0939fde787160777747955fb58f890ece54d70552df65458ccbe4209",
    "page_script": "e24fd32256aba2506b54011e8a299627ce87176ca662fede348d222f2030595c",
    "seeded/index.html": "b46ede0dcfed4ebab06ebab32c37b018bd687f91152f894e398f188ff257a717",
    "seeded/job.html": "7dd58b9b6f95a4aa101ad8f4b72bfdf4b1a4fbf48f95bdb8e4d560f0117aa927",
    "seeded/activity.html": "970c1023daf9fe7250a692613baf284672f501435b9f6ee28250ccea84882ef5",
    "seeded/efficiency.html": "82d2e8f5a9685178fd1757bf5638eb26bd7d9b04949288e01206c8ecf9ec04b9",
    "seeded/health.html": "4afc3d43656d94d35927acdceedc6011dce75a66183464f0749d260036235b06",
    "hostile/index.html": "909ceaaebf1fc4362975f2c3995c5026596b168b725c0c3daa60e87950201220",
    "hostile/job.html": "9456ab78493da380d2c2cc38a80008351361d856e47c66cbe43e1df23479b55f",
    "hostile/activity.html": "e380986eb6f5137c7c7d43d9a610fb2a4658197a3284dd0e9fd6a3105367ae8e",
    "hostile/efficiency.html": "edc0e0db327f4d727cc75db6304d658ac32feb8c2de01c7ce9bd97cf50fa2b24",
    "hostile/health.html": "6bfc28a53bb0eeec226430bd6eaacf4fdcdd70f84cbf0d4936d6904b102bf2d3",
    "empty/index.html": "f1d62e427fb57d91beb2c1af1a7e64e80171fb7683c068f49443db2034045c53",
    "empty/job.html": "17615b91025e4a479386971030b10bdc99f8696d736fc5d331c6dae85e362940",
    "empty/activity.html": "64e7783c168c1c305862746bab352c26f5c2950c6073448fd651a566ead664bc",
    "empty/efficiency.html": "7c667019885045f88ceca87d456205ad5107fbff05504ff8a4de14c8654531bc",
    "empty/health.html": "8b46413dafb285b885cf5a84cefa01ebf6e45608922ca785b4ed7ee70c0a0de7",
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
