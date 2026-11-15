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
    "CSS": "ad6ca7dc83d799e622a12c2fed06ef9175dc2a83db03a2e8c59247de64d247e4",
    "SCRIPT": "50031c12aef53a710ea3cd26826ddd552429fb07a77264f4504fba9148f0dc11",
    "PX_SCRIPT": "c468396e0939fde787160777747955fb58f890ece54d70552df65458ccbe4209",
    "page_script": "d3c584a7d2b80100aaf336356359f8a4c8aab3d4b13e5b682856200bda94293c",
    "seeded/index.html": "b35ab38d2f2f6147de8630f173371da4969e9af41c8bf9c7f15520b499105f07",
    "seeded/job.html": "918efcda1a70f40fff4d06b01429d5d7f6f3f895494a7aab575994359abd4a58",
    "seeded/activity.html": "ec42df3526efe14258656d2a76a1c92580426903af663f1499fc5fd9c9fbffa3",
    "seeded/efficiency.html": "f3e69a6a6f776fdc3804f5ee62064f227dd4b17dccd78de1a5daa4bd2429acbf",
    "seeded/health.html": "1db530dbe649c50315080a401aa1be27e6de2e01c019186780041caddb1701da",
    "hostile/index.html": "e6fef60511ae24151006935c31a110ad89133915106d506b8d5469257691ea57",
    "hostile/job.html": "f27d98ebc15c661d85020872a502b8caf63a14432ab5b47ab1f946369c446bdf",
    "hostile/activity.html": "fa7bde4084f5e3b2c2e0faececa80726b924fdee6b0fbddb8ce6ce99f904e675",
    "hostile/efficiency.html": "a3d2688bd7f6e016fec503621ec1c2cc6da0f055e8fc17c7e6b9aeadcf5809ae",
    "hostile/health.html": "81b2f3c34ce3509268e1ef86336f905d83ae3707691b44f118763d6c99499d42",
    "empty/index.html": "d0286d865322992d6f82cb51c020df517c90e9a87357787d7ea10308451051d7",
    "empty/job.html": "c399913b7016fa4ccf47d2a568fd022b66765bf849e9dc283f44854e57acd2f3",
    "empty/activity.html": "19bc8a5b03a3a7d604f06cbaa8228e1ade45049f8d08eb6ee1f936401603e601",
    "empty/efficiency.html": "f3b742f9f6c20735015e79dda772dad7235d0b22a0d52d273b74ef0757e5a388",
    "empty/health.html": "89bf3ec3321e1f3ff9be105a78b18b17c09435b303130351bbdf77ec181bda34",
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
