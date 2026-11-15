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
    "CSS": "9c83e2a1794731e311a8967b86fa539e5cc79ec84a578b2fcf115df75144c5f3",
    "SCRIPT": "cb9739e506be51fa2952e891609a39ed1efdab70b50dc985f3d2dd713eafbfa9",
    "PX_SCRIPT": "eb97c011fb3b14fe54d32f8e5b156faad6aa1b619c1df8defb1da936a7f80357",
    "page_script": "101e0d42d0d88b28581395b9627d55a1bdd286cb90cd318693c263603a210f62",
    "seeded/index.html": "497c5e3154742f1d8f9e192e637b919d37b4de2b90667b3dcfd810287f1aac40",
    "seeded/job.html": "348c93bab4b5de493c8c064d587135031f6e2ca3d4aa57acca3ddd2119405247",
    "seeded/activity.html": "9bd8cdbb18ce405b07fa4d8120a9f734143c19d5ba61c1c9a1687a5d2a9e0ad6",
    "seeded/efficiency.html": "6b8a5effcc89f663d3445a16e5ef71e77a66c184e759f67c4012fef7ef395cc2",
    "seeded/health.html": "c0cc36ce050128b8f2e83b48adc6ffe09d52fe75d6769faa58da9083fd0a3e47",
    "hostile/index.html": "9c515496525bd0e26735e8858b1086241606632aa594f03473a5c4473d81291b",
    "hostile/job.html": "ea1cc7c83a0e4987cfed8b7612879d72bb4820f0f5f45ddbfd5836d2e1ed90fd",
    "hostile/activity.html": "69e852dbca3dab9563ed8630a5b2b00c53a0e653a9388df04201af7a58c0c06c",
    "hostile/efficiency.html": "6b41bbed413d3f97b31ff21a0bb953b722e6bc4450823064915a0e4cdae50584",
    "hostile/health.html": "df0e7cb98680aec68c0f0b34e73713b7639de0b5123c66a6defa64922507b4af",
    "empty/index.html": "da3c92a2fd6dbfad991cbdcf2b746484b73638d5c01d53a20c68697a7174fcd7",
    "empty/job.html": "795f59ae607e4fb533e525b972c262fe0479a3f0fa6b984d753e470034650b82",
    "empty/activity.html": "fc4eb5823f3cf71c195ee03b26e9c4525c89f1c8ee07efff3ce04c0d988b92f2",
    "empty/efficiency.html": "818c978d713720f4e09e643897f5b3d7cef3fa93941538402c0a47091e193d98",
    "empty/health.html": "d8ea91e6cd418e117267d7bd8040e11d6a81470c812a9272fc559c9549da43fa",
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
