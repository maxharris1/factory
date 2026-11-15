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
    "SCRIPT": "bf305c862911ec5eec1178dc44a465e2fd27946a9134181200af2512d3abb9dc",
    "PX_SCRIPT": "c468396e0939fde787160777747955fb58f890ece54d70552df65458ccbe4209",
    "page_script": "b9225d2ead1ce3c95bf724aac35496e74f00238cc8d882955ed6858d9abc9c0c",
    "seeded/index.html": "0377f914d03be6ad16e70b10c63599fd58ee84b9ee690717eb0e6a29a68d8a7d",
    "seeded/job.html": "2d81ca5a70b79d835ce4c67b6d9311b6fbabe3c471f3467ba25929b55e33ab4f",
    "seeded/activity.html": "0e2ef03ebb1dbec27daf04de436ff8d902637b5851067ad4691d0ee58827dc26",
    "seeded/efficiency.html": "a8af3249cb6053e83be016028305fc2b7a78c77cfd32fceeaf0707bfc417f6fd",
    "seeded/health.html": "4b0d839b35f2865c07e4880c03c63cf42ce2b3d281e827f7469c3f53e664a8fd",
    "hostile/index.html": "d825882a031c7d6d2b5bda8b89f42ce4a23e8befe0202d9f1e3c8cd40557266a",
    "hostile/job.html": "4a3592fe46251c1ce80dfe965e3883638121316509e64c69a119fb30b4f64693",
    "hostile/activity.html": "2d8b62d97f7508d0bc512192a8a15525b5b63d7282d416be99fab4d92b3c7d6a",
    "hostile/efficiency.html": "6ccda80fd676e6ddfbe461edc784df7e17a8a7831b5d5083e8d122c73587ae82",
    "hostile/health.html": "d60969b7493f2610544b71f58a678fcba422ecb925871eaeb1e7d78a3021ef92",
    "empty/index.html": "1a6879b2c76796ddae7fe6593ebc75375274286e8a1ee3727439bb6389383259",
    "empty/job.html": "d8a56475ff7e79d103a7e02d3d105c3675a459a15f887692cf958ab5b83baf99",
    "empty/activity.html": "50dad78243610c09bfade47ea7603a19e0b59849501b8394e8c579f100b6576d",
    "empty/efficiency.html": "ae20889708867d0d8c9e1540dfc4041e85bc132ba3786689f7ab92b5f2c0268a",
    "empty/health.html": "f19729f2c67a27d5247b3c786c25e57122037d0100378ac98b2a7b03d3fde7ea",
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
