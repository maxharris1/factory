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
    "CSS": "978bb17a7c3481a60a2c4064d6e46153f6e0032f2a9ad0997b2bef9281eff9e9",
    "SCRIPT": "c5991fcffd8f6a74c68752255397f2396990bb6a59ad59233c6cac55f6f6468f",
    "PX_SCRIPT": "b9353d15709b8b09e1de473712f8b909633ebe0cdfda2abcd987acf9b62a92b4",
    "page_script": "40f90525042e572b979fb174ef02de5a7d46ec587bd141672635d5f44a2ac0b4",
    "seeded/index.html": "93655315333278f8a240e1aa24b52e33a1c62f9e9b90164fbf97ab6ff6e27877",
    "seeded/job.html": "ee888ff420202166a24ef5ef1033b350cff6fe666a33d77114054415d7931014",
    "seeded/activity.html": "28d73bf962d92e72af75c45a986adbc5374cbc57b813f36cf3ca63096dc11af6",
    "seeded/efficiency.html": "d5fa0c20ef23ef3b04ce407b888479abcdcd4c9ca7a348bc84c7da04b51edf83",
    "seeded/health.html": "3e6c7523a27aab83527cc124581ba1a2bdb68aac8dba8960a35697efaa4bbf54",
    "hostile/index.html": "342033348b548716c99e3099b34714b7d1da6ef8c0cdff26fcf96f4a2c0e3475",
    "hostile/job.html": "b5b99b065f3b41130a2935f34321b95c4026a9edcf809f17fce5abd8651cfd7f",
    "hostile/activity.html": "065ebf7ffd91dbbe8d88c43cc1dde728cb86dbe106874262fbfba340e6d6cc52",
    "hostile/efficiency.html": "db3b4fe02c4224b7167a9988828a92784ddc7e6ade5a10869f7508beced127ad",
    "hostile/health.html": "f083eece03c5e708b464767f5c53b49327c9106569aed96a20e1d0bfd3a7bb61",
    "empty/index.html": "46c1b629c91578c9085f42566a3499c23941f6f8db1a4edae7212a5b1f03287a",
    "empty/job.html": "9ec116867886352fda36b94535ce8acd062672be2c9fb348dc5c527cc7e8a048",
    "empty/activity.html": "37f58ca0cf450f883a8659dd5423ab7b84b32d4bf9cd273f1e03abf44ba83595",
    "empty/efficiency.html": "fc283d018814cc3af0d406da8f660a6ef5ca60918e6898441b9c6f5dea379838",
    "empty/health.html": "34d695c6dfc719118109e1b3452fb9d5c34ce14839681ddf96fcbd9699b24f3f",
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
