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
    "CSS": "c819e8de7685b4a4ebc80785ffaf90496097be44791c952de6c037d441ffbef0",
    "SCRIPT": "0dab3d059424ddfd7dee12928cd93fdd6bfa404787d09682f2eede7bb24e7512",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "ccae4a05d966f65be0379e2c716e706c319a7884bda6af2067480b1a33335b5a",
    "seeded/index.html": "a5c2671ba911bea43ad9612e91384e8002fe18aef8f749b55c494f05ad2a9b86",
    "seeded/job.html": "d9c1d846caf57cf243f16dbbd08948e817d1d6ab362d895320243b623bd8b123",
    "seeded/activity.html": "7c7b5ccdc7670744845b8856992e6d1fc4d27798f0af02a701c87bc8509bfdee",
    "seeded/efficiency.html": "d81c8752cc6c25cd0ce33a5e99e609262d5d15291924f4c7793fa92ad3332e0e",
    "seeded/health.html": "324411185e95658184a5cfde4bb1e724abebe8f7721750144c8486b2765718d2",
    "hostile/index.html": "94de4b373c012dbd62b17300d1e8e9f9df3278d51c5bb1b95ee61b1dd16912ae",
    "hostile/job.html": "d02c09b2d8500b71fdd5c806722686038ab8215622d604dda67e41576f79e4e2",
    "hostile/activity.html": "2410de6321ed6335a19324f51e09526ad70b66506e008e0469e1a3fb1a69a706",
    "hostile/efficiency.html": "bc277568098009ff9e1e889a6caa27401ce61951e62b43f64bb86c533a69d5cc",
    "hostile/health.html": "45acb640b357ef44cd529b8332ad2371f6dc400a872d2d204de877db2511ef9c",
    "empty/index.html": "b6022a5abc37c9a4640b500b9ecf6c9a95e77ed486d7117ef2ac1f0f9ab4c0c8",
    "empty/job.html": "fc69d7a2c6490805a6aade72d281b3128dbc1c450539ac46fada4abe9ec1f1cb",
    "empty/activity.html": "a12bc956d6a9ff0e8116ab985049e0f67f548c75b5013f45c3560846144fbc9b",
    "empty/efficiency.html": "84b303c79232ed324141c4b8aef12a3516d2f5018b703757955d97fad6e680c3",
    "empty/health.html": "110ae13c608ce3088d4f95d14ee856be6e6d5bb26f82fe2f3e31a1ec38c9d0ee",
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
