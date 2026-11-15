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
    "CSS": "19a0b1f9f72d20351073fe719ac4971cdef5c2bff041dad108a8a8133b636237",
    "SCRIPT": "19c15ec27fb236d640d048344c8846549a5c55de8c247a6e939f9ceb13161190",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "49bc6d39ccbd7714af3f03ac9e2cc78ae4a6829fb9ce738dff0446f86b2518ef",
    "seeded/index.html": "a1f6a4158bfa2d909527aba10ce9e974d8695a53820ebf73a946cc603baf7752",
    "seeded/job.html": "643ba73df565f52171b0bb7f6df6dddccefbd3f3554adff21ac21427d83aede1",
    "seeded/activity.html": "0681fb5e12a7c2951bcbc02d99c8be259a5b255bb62f1d0d06db6512840e161d",
    "seeded/efficiency.html": "944804c1ab3613e30adce5f80ad3dd322c44e019805b676d9afb5320a011909c",
    "seeded/health.html": "b97530084d28818af0621c37adcd413e085f382abbecdf73da74361a70277f88",
    "hostile/index.html": "383998537942bdc52f152889b73f69b03724ccd78b66bb703c1fe7dbed26810c",
    "hostile/job.html": "0e9cba899e180c92737898f091992ecf16183fc9bf075ffd1367197b3bb7db81",
    "hostile/activity.html": "a924ca6ba832d777d7857b8527cbc2573e08606d4a50ee77b2b6e8cf91e113e8",
    "hostile/efficiency.html": "c1e85d61594eb1f5f5bb6de1962d3b2d4b829d6476c5223bf02cdfadc6297765",
    "hostile/health.html": "3c986709c17fd4b7af1f6cc17e39312d4e6a7255c558612a646b88aa60acbf23",
    "empty/index.html": "10428b9208648a65b1efbe2ae0016ca339d44a68ceb1198daf87450e8a64f7b4",
    "empty/job.html": "44a4c6a9222664b980c674f1713140f4fca51cbb6eb5ad2ceb0b7cf6e25c398c",
    "empty/activity.html": "0824a38da6174131b038c2fafefbe9c6f6bd86637a26ee5332436e18b024f53c",
    "empty/efficiency.html": "eee83e967b880a7bf6bd9064878fa23852562f7458c42636572c928ec9ace80b",
    "empty/health.html": "be24b887b1ead47fbaba4e73ae36fef0c543d00e5026ebe609ea93b94b531f9b",
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
