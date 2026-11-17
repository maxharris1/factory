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
    "CSS": "dd77996aca8c652b1c936ac579a438432a1f6804782b81616e19b02d6da65d55",
    "SCRIPT": "27dbba6b9e000c0f98b13e8d5d92235b86b1ceedfdc6002563c9b52e966881d1",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "page_script": "75fd23fa78126db6310cf63f6febba11211ed7133f5a0482af32c1942d56070e",
    "seeded/index.html": "efa46535d880552cec378f14ffdbe37219b373a7fa7abb4f4ca2610061299142",
    "seeded/job.html": "b7b81da60fdc92f169b39b392073ed8236a5c3aab735ef6c1cd3cbeacf06b2eb",
    "seeded/activity.html": "5a8a4c7dae5cde44b80b01019822b07bd11f746d53631bbce8f13cf81dfe308f",
    "seeded/efficiency.html": "bd25dbba38b0ce2af5463649815f6aaac8f152688c87c299da6fa4f7f9684ee8",
    "seeded/health.html": "be430c2d19b07481c16580afbd7e99e614ec581467289b922d87646bed5301cb",
    "hostile/index.html": "e6ac5eff33ee2c5c400b2b875c86f170b0adc73e6e83b4d744984e28050e45c0",
    "hostile/job.html": "a650eca6016b9c06c9b379ca2b030d24632b47f5f339af0dd7d1fd12df78fe75",
    "hostile/activity.html": "20ec42ca486930263d75ea7aebf272e148e94aeb0c732dac4a5cdaa27b34b1e6",
    "hostile/efficiency.html": "8ce97d247b238951ac2431d0c5894aa504da4c22b59314679a3d431f00ade586",
    "hostile/health.html": "1e8e0a9004690baaadb5ee4e71360686bc01f335b8d72ff2eaaf27e919f3e19b",
    "empty/index.html": "efe9e9b85dfa2e777cb8e53c51ec3e45d7dfdec53fd8579b3c11bcaff5c34a31",
    "empty/job.html": "4c669eb10ca9767e59a4069ed750966363f55e4c16026e8c6f27d62b34a41ec5",
    "empty/activity.html": "f01f281b6e03877f1d64ef05c0df4f804e6b07153c8496b5e65f364faf3a5a2e",
    "empty/efficiency.html": "7f17e9662a48e3ce88dda549d9858f58331727e8e4e85ba3b4b1e51013635ff6",
    "empty/health.html": "f1076bc4363fa4ab2cbcac6d706d5e055e62b7578ec6d0568bf68e538cf9c2d2",
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
