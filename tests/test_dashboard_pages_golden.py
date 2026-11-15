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
    "SCRIPT": "f33751cd223ccefc3fc8fa8639b169a0b2dfbb225a37ed0adf2659da2d150557",
    "PX_SCRIPT": "c468396e0939fde787160777747955fb58f890ece54d70552df65458ccbe4209",
    "page_script": "56769e6c48f6937949128d567d6e1d5a08f01ad5fe109ff171ba49282f541c52",
    "seeded/index.html": "60e37499ca50720944dcf905885b376b7dc810b99362e2787d067143eedc8c37",
    "seeded/job.html": "e3c0935e72dff23ef72890603ea98ede83de0816321eae13a53db787fd5e8efc",
    "seeded/activity.html": "b331bb51d599c70b27d85e3a8e81640dd76a60d412c1d133dee620362c61aa37",
    "seeded/efficiency.html": "f37d377f75775e3543104e59be8ce8a4cc35628a59f61ed8201ec722d0d7ba1c",
    "seeded/health.html": "65f810292f95bf7c02ff53bd8639df51c01f082a428947bf4220f6cbfabb2819",
    "hostile/index.html": "50fd74df8094160ef586474fa9d26e170fb2fd587120d00e029ec752d53d33b6",
    "hostile/job.html": "121f26c517906091742b783e2fd51bdac026d4980c95945922f2317e85103beb",
    "hostile/activity.html": "75f18bff07eb9f9e2c54309ee4da4da2b3e809e50a40a8491c2cf2d003d5abbe",
    "hostile/efficiency.html": "5f7dc33e080f752de956338cbd8b8b5b1e61d73383e072b613496b58d501b104",
    "hostile/health.html": "8f718417f66720a121fc2bf9fa152d13c0f49fc4cb3dcbe947c2231760986a93",
    "empty/index.html": "97053736f1c19bc347570d2fcee107a4871a8de227d4d5491351215c385e7986",
    "empty/job.html": "7b5c995e0a0738611adf09d82efae1a00d3589fc9c6195c3416e9001da4310f5",
    "empty/activity.html": "92d96518fd2912a19d2e145b277f02c62f417a7e5177f06ea3d84f78493d5b85",
    "empty/efficiency.html": "00c5cd6d72395211819f5d2ab7f6a122cf9507a5968711708d57ec3fb10c1aa5",
    "empty/health.html": "e4f8ab4037a5779c26cb19dc5b7b13e5f883bb36d2ff03c1d46cec1c51e374a1",
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
