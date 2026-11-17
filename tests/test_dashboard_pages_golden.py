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
    "CSS": "8e5b4d337333fbcddf2bbbe6e97495a9144ca7f9b5be9af69d17b0d6bb05b31c",
    "SCRIPT": "ce193367d38acfe51006e26f93b5859cf18a89863a70ab2346905480e784afe9",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "571ba3f5f7c5e4f9a7ec7238dd12c8f4d2b65fb4dc9daae9439d6864ca7aca7f",
    "seeded/index.html": "5d316947751aeaaceaa2269fa19828aca31ee5d696b1f717c0858e37e7c0a40d",
    "seeded/job.html": "03b3d7d501c5a72ddeac776c547fd918aa3c69c2e8240bccbb0ebc93ecfca7a1",
    "seeded/activity.html": "ac937e481d567fa11c1ca05c912492311c001d14843893aeb78787f571bb54f0",
    "seeded/efficiency.html": "abc67fd66d90887a0c548b7c41faafb11bef5c429a159fa610361bf569574cd4",
    "seeded/health.html": "0d32c6e81741d79dadf056158cf4fe3272eb0fd155baff46faf5f4a8354f6a12",
    "hostile/index.html": "3674da5b22094418cb83b6a4886a7455778b0daa15c1e3175d24f2f6876933f8",
    "hostile/job.html": "ab59359fc5e8190dd44e8167e97b83b3c4d442f5faac05426408fb5524c09254",
    "hostile/activity.html": "907ed7c83e06df03ac6da8fddeb4fe9c8cb3ea66c9c12dcc4b2c3129f8df964f",
    "hostile/efficiency.html": "f7dbfe3d40ff4278ead28424dade399ff3c842e917059a50c656be53208e8581",
    "hostile/health.html": "f2cb8fe5c73a179bda0a73a90b69c8afccc7c26750f1b238a5bfc122d8f54be3",
    "empty/index.html": "ea23ed98754af730ea936040dbda0ac3c9f0799ab46766bcacd98e802fcd3072",
    "empty/job.html": "820834d15eef45209bc9218f65c94b5dadc6c062f0269e6121dead91cb1ac7c9",
    "empty/activity.html": "c0b827fd3b2af8a3e9e76d7c5b2cc5f750d0502939319fe33d0c91e0266bd19b",
    "empty/efficiency.html": "e4478dee1579b326421097b9c1765d39043104224cff8fbd81dc58d018db3849",
    "empty/health.html": "cbed2a17454869454a24e59809b87a289f2965a062218e24232df201637873ab",
}

NAMES = (
    "POLL_MS", "FONT_DIR", "FONTS", "CSS", "SCRIPT", "PX_SCRIPT", "font_faces", "px_script",
    "page_script", "PAGES", "_PAGE_NAMES", "LIST_MAX", "_BOTS", "_e", "PX_EFFECTS", "PX_ONESHOTS",
    "PX_BOT_TINTS", "PX_FALLBACK", "_PX_BAD", "PX_BROKEN", "_PX_LINK", "PX_STATE", "PX_ACTIONS",
    "_PX_HEX", "px_table", "px_state", "_px_progress", "px_frame", "_fixed3", "px_strip",
    "px_job_strip", "duration", "_num", "_short", "_ticket", "_job_label", "_job_kind", "anchor",
    "_b", "_span", "_bot", "_chip", "_panel", "_feed", "_when", "_topbar", "header_load", "_band",
    "_attention_strip", "STATUS_LINKS", "STATUS_ABSENT_LINK", "shell_table", "version_drift",
    "_segment", "_count", "status_segments", "_statusline", "_FEEDER_STATE", "_feeder",
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
