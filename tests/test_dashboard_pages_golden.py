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
    "CSS": "625f21d3ebb7876fbf061b3aacb2c2a77c89f3319334f1522fee3009348c1952",
    "SCRIPT": "6ef17b9297c6f6c3a80cd0df10653a97e92681feae0b1a22be1c47d0da819e78",
    "PX_SCRIPT": "ba6cd972284b80cd4fef5ea3516586058e3be22b65f4cb48478f60e36ea47e3b",
    "page_script": "e5bf21fba2aa2808eccc0012c4018526a6d9cc62e6e8800f4732d44a7116f287",
    "seeded/index.html": "f97b5417b901f6801798ce0d56e7634cef08ed758cd19dbaefa8866609be371d",
    "seeded/job.html": "3aa21d91903fa1494d1c68055fd939cc8f2f382b398a970cf3ff8ccbb7768a05",
    "seeded/activity.html": "2969c75843d9ceec46d05e240e638706c50b0cde81c0faa57af06cd3d77e40cd",
    "seeded/efficiency.html": "8be9b25abcb204b513766783b27790aa07e03232a17776769b21554d221e0904",
    "seeded/health.html": "2dcc78f77f11939494492f863486641133c90fc96d93a1f465d560808a00af44",
    "hostile/index.html": "7225591e45fc3d03b746c56806637a1141528faab063cd619847c9c1a687cbd7",
    "hostile/job.html": "5fee3a2567e591820df33513dfcc69e3337893f1e76d56dba1ecbb77b05d7779",
    "hostile/activity.html": "c8f3fb22632cb0694f51b3159394f17e216ec5f8c00507fa54c61206ee065803",
    "hostile/efficiency.html": "86bcdf8c9460bf6fa290ff0b39fc9ac23f440fe4a95209f41997ca544318a697",
    "hostile/health.html": "38e39fd7fd82caf448a77deb47f337302c46a7ea1f3cd8d25af1152c9f15582f",
    "empty/index.html": "c303853c94a639cbb19126581b6b21b7f36e4710d27f802ccaead0e115e21f91",
    "empty/job.html": "fe2bd47525f44235cae73dbb7982d31d3c23ea055d807ea51041be96200a44b6",
    "empty/activity.html": "b1486457415dc25b3d1c8d81e35b9baf357dbf1cf32d59322ea9b687254d1941",
    "empty/efficiency.html": "ea71b9892159e2c1b47f3340f7035f363f50a17e206e1ae59264dd9130343c96",
    "empty/health.html": "998f0f53268d08dc61def8129244db3930f2eb92c9a2a592097e2cb511c1dcce",
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
    "_savings", "_active_jobs", "_board", "_detail_model", "_job_article",
    "_jobview", "_PANELS", "_page", "render_pages", "TELEMETRY_CSS", "TELEMETRY_SCRIPT",
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
