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
    "CSS": "d4df591e86666a87dd1943df050559e109c2613b37d9bdf5e6094dbe3fbcd346",
    "SCRIPT": "6be144baf4812a16688c9794ab6f6c984658ad387d19d8b570da633a69c541ae",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "cf4f542da1d34e78282e0ff8a360bbd2639cfcf37e99b4d455818f75bacb5a32",
    "seeded/index.html": "88e0ef12c25e632d6eb1f56d68def92a3090c805194615a7c86b5191c8006ecb",
    "seeded/job.html": "5cd231b1826967d40a61a2e680193b3cd4d9429e84ca0eab66b44c12d7dbdf98",
    "seeded/activity.html": "a45eba5ba418477dd7f10097b0746a77267da5a7c107e5b76a0da9c31f2d19ca",
    "seeded/efficiency.html": "84ea784f3e8bcae0e21c540bdcac69f727be9fb8d04a6dd5a56846a2b12b0bbe",
    "seeded/health.html": "30df10177081412057e5f817ac9965d0c1dcf90818b4aef4567fdef40f170d46",
    "hostile/index.html": "ae4b73f2ad282258e4657786ca667a53bb3e819a48fe8cd19c2068047444d23b",
    "hostile/job.html": "e5e7dc94daffc05374ba549ead6bb55790f7256750a032754721a34a71a47b46",
    "hostile/activity.html": "81244937f7dc48d329e5c415f0a825a8e8e2293f837442ea8562fa152b75eb33",
    "hostile/efficiency.html": "775aa389839a49e370645eada6be46c819570673a98e677c7ce0108b6381aaf0",
    "hostile/health.html": "a90eb35acbae8ad44278448ff4414d29113a1fd117c2bcf9d675e9d92f5d7a74",
    "empty/index.html": "18ae462597c3095568b94fc1c6f3f3d3e81a27d4f04c7a6212af06aea6835795",
    "empty/job.html": "49691574eee4b0cce0260ed5cf04a29df4578107f6977ac80437e71fcd480f62",
    "empty/activity.html": "99a7df6e5f9318ed48620ed323d1217ac0ddc1bb7a3b95d9c5af58431c7d36fa",
    "empty/efficiency.html": "0a5bf75f95fa73c79ed4c22dd02c9490f57e6c7a1c673da9eb4dbe85b92fd1f8",
    "empty/health.html": "1c3c411d6eb2916fbec46bdc110320476fbeb7c3641e5373a6073c767f2b3fdd",
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
