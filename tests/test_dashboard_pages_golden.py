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
    "CSS": "8429f8c8059df7218cb5f76c54cae29e0857454329540bf88f55d7fe1d9a373c",
    "SCRIPT": "cd85dd35cacdab393c79de66bffd84653bd8da3ba037b328358b864b716751e7",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "028cb50254e0d6d579c20dd6e34ec297fae6cc0fab844365a936e8de74bb7200",
    "seeded/index.html": "267f137fefe819417ec06a290b2194cc243bed0ed313aa319bac46db2def197f",
    "seeded/job.html": "94485c54b3c3ee364ace7c48b89acaef25be220433c2899564b94883825cd29b",
    "seeded/activity.html": "d27fc45173754832ad31b2e61917294319281a4df0064e2873bd755cb38ce6e1",
    "seeded/efficiency.html": "b4155efde18cc9d2b7049a41296229c391442187fc1a1ae4d30408a0d63166f1",
    "seeded/health.html": "9bfb81999d0f1b99e7af804473256eb710c4e66986a63f1723d76f6fc391c7e2",
    "hostile/index.html": "00af55f0c0fec5f1f7332c93644bbc539f6d577bf5694e39527271db2178f80b",
    "hostile/job.html": "5647c7c64e86d187f365ce69a294d2b6970f4fe12f5f848ea617a32c0794da82",
    "hostile/activity.html": "f4c0c277372ca7b3fe4633f93509219feeab97ed2287303ced9f198bfdda2df6",
    "hostile/efficiency.html": "53a32d50b22337276ba79c481c2280d143dbe4faab83d4789eeeb623877c691e",
    "hostile/health.html": "0d73e0d53e578cfc092271d14e618e48303e29aaf22e69ffd3494681561122fb",
    "empty/index.html": "60430702164f61369cd8cf4e17f35265e0ad8a0c513429e785fff5beaf36af2d",
    "empty/job.html": "05018ce016c117418c43b349a986fc9cc006a02d68a9d35f6ea3d67c2fc3e7f6",
    "empty/activity.html": "99228891e75f8aef57e6f95ae79b8c261ad8267a156b00717bb527adba1af616",
    "empty/efficiency.html": "d236c0672b36ad8353715b6430dcf4f43b7e4738495feb917ccdbbd6f93fb17e",
    "empty/health.html": "1e3ea87e19ed98d823812d4549406f6a67733ea80869c61eea1184191bf0bafe",
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
