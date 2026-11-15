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
    "CSS": "ccc19574a7aa68479a72697e5560139b17f25a9a35c56d2e3adcc575c3505e8e",
    "SCRIPT": "27ccc7968b3f0c9cefbbeac517ff2e0a3d000e658f85d176355033ba2b11ce98",
    "PX_SCRIPT": "d3c012a9dcc3b97338a731e172ea14cd5d25dfc59c7fab2fdd867bb974b9dc1b",
    "page_script": "3693a8f71a93a4157c757e01bb5e04ac2ab12b1110bab9603e7f468fbd00ca82",
    "seeded/index.html": "9461fd62a1a64f4597818603149266a4bcc17eabcdedfbcb4325cf6c3369431d",
    "seeded/job.html": "f4ceeb33a290f66f5f7d8bf791851cbb1c7004d88f535b4d37902d24abeb18ef",
    "seeded/activity.html": "0c37de7f2fbae8c1acb2857b9e51e1628d1e17a16bed6a6ba9a5b9454ac64e36",
    "seeded/efficiency.html": "7a14190f7773dd3dd5cdf26968013574f5165b2afab0964f074ddfd195ee58b9",
    "seeded/health.html": "3040f0bae7031fde90a82df1bfc5ad0cf11065a47db314211a049ce57a9f16f7",
    "hostile/index.html": "84c63c636371c97d3788d470e92579e1d0bc8c3c6da985eee1f7ac723fb08b78",
    "hostile/job.html": "d8a5505b9ac54b4832a3e0c5d290186dabc9aad1f69ccb09f36ecbfda650ede3",
    "hostile/activity.html": "df269f7cffaf6e28c9282699974078a1fad348e4799afa40ac43909d5ef1a772",
    "hostile/efficiency.html": "f60f5260ec560688a92fc05c8b31a979192cdb1d5e233713b19d50ad58a3ddfd",
    "hostile/health.html": "f861e571da561857da7daebf8b0f30931cb9a35add1f6c9d88e5d029126c269b",
    "empty/index.html": "0165fb5c41944816f3734e286b7edb07021ce698125b3100681195fee8a12bf5",
    "empty/job.html": "2b4ba897fe9a18da4cc1ff77dfd0a1bbc29e267cff772fefc8569f1670977c8d",
    "empty/activity.html": "b0c5a21ec1c648e8c177c75fd3ea521aba4e02d13c29e879720be43e751bed22",
    "empty/efficiency.html": "3245b3016a8587f84472e80981858719db6e87f8119e48d141bf1c57089410e8",
    "empty/health.html": "08bc673a695727605df33f64aa1ab8995b25f4634144fd5786cc6fa19464e631",
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
