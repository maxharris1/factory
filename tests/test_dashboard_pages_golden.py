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
    "CSS": "cf2073bc2f46a7ea628a7f98c3eebf7eb906a6e423a8129c661ee55592714eff",
    "SCRIPT": "2ed6d3276e88256a3f39360e3b7976bb877eca92540cef1afdcbcbb3ca885fc4",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "429930e20dab30a9c8220c2b09a5b68845117ef9240866665dd7dec0d01c7adf",
    "seeded/index.html": "21b339bfa3f74498f84d4c4c2cce76923f47cf9a999e5953d724f2149f2c4cc2",
    "seeded/job.html": "069b6b00d5d8b88ec3a58bf1f31ec6d0c30027b971adebe1cd21e9f0e3984417",
    "seeded/activity.html": "638a32204f2d9f40f71054964e48a67d9c007b8d0b0174bcb65e9c9da45420c4",
    "seeded/efficiency.html": "92c625a1d0ab3fed7abd8e2d7de5c9ea599de2a4b50db16d21b91d5139908b35",
    "seeded/health.html": "0173720882cf0029549b694c84f0aa1ad3561bce693be0ce9858fbd4bd2438f9",
    "hostile/index.html": "2553732df41b161afddaa5eb4a23211fcb28d97e2fa45049f67269609c4ec840",
    "hostile/job.html": "11c9ede81fe424691e1aff513b752242ce9bb919ea975a3812742a3d69d60b29",
    "hostile/activity.html": "2878237f933a9584883892f399143824af7bc685540f9bd9e1017d3a28623815",
    "hostile/efficiency.html": "4b63d056b941a50ffbda89c4c2b05ead726ed94023bcb998ffe5ecf7a8c87b20",
    "hostile/health.html": "7c0523b1d080001e499a0170d5b1b407117cf153b0296fc26dab1ca5fdda89ce",
    "empty/index.html": "297803a7fb4a628889bfde72a4d3e95d89fb19a3ab0ffc05a7038c76f4c7d106",
    "empty/job.html": "4c6646d13d3768a821dd7f16c1390dd41f950e02d684fb7f4db3a0cece07b4e5",
    "empty/activity.html": "056407aaf89f7f5d1fde334d0777b04dcc355df6dd00a78966ddb1350ae658da",
    "empty/efficiency.html": "a7dad4d6bc0a52b13f55baa92d96f021e5b83d6d378b38115adcd4dabb71aa33",
    "empty/health.html": "586c114f3a00b26c18ecdfd86a650d433d6d90d6e2803f8485c7b22a84403bb9",
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
