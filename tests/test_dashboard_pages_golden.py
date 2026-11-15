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
    "CSS": "1e8d4f50d46654090e6c2edd9dc549bcdc654f29e68a3b1ad07edc55cdc0c80d",
    "SCRIPT": "7a374647f144bfee4146697d15f467b80a52ddb95827eea491196fb7d9f30c8e",
    "PX_SCRIPT": "ba6cd972284b80cd4fef5ea3516586058e3be22b65f4cb48478f60e36ea47e3b",
    "page_script": "08139137eee4610e8bcb032ba5da680fad1bd62a7f818abc954d08806aa9441b",
    "seeded/index.html": "38be69e7547b69372e514507bcd511992cc30a70676e34529859f2796561b6f8",
    "seeded/job.html": "cb3e406be6c9178601a63e93f1764acb4313ee6dbaf937e7943ffdb5c787b984",
    "seeded/activity.html": "760cdea257f50e30caf8ef4ca209e02c0a3ba6a64e95a7a879472febb7f0f990",
    "seeded/efficiency.html": "11bc6509c1925d07f2042ddaff105ecb3a0904b1884770a544afe450215a0135",
    "seeded/health.html": "9f30f4a02a8ce06ba737d4d986a219de209870f4248bc76142bd630743b51a72",
    "hostile/index.html": "4e2e5f78d279d77eb1ec570b356ebab24f91c1257be2895cbcf138667af7a3b1",
    "hostile/job.html": "7b403daee6f014b2e8405c744fd1f0ff801640e8c06d19bb45179884f6c72baf",
    "hostile/activity.html": "c9726429e842a8394837ca8b063c46b0ffeccb3327331ac7d48df9b12cf70e23",
    "hostile/efficiency.html": "b5e73053f2c6261462c5efde0a74d6f0e4a75f99eb4f02689b0f2eadd10dd8c1",
    "hostile/health.html": "7c9124b877d5149aa25213b83ecdede3b35f90fff0dac143f39c17fc59a14bd0",
    "empty/index.html": "f88adbbf864f5b0a45e0d87db05a44aca9dfd0dfcc2bd5cb5f88b026a02cc129",
    "empty/job.html": "a40708492d0c1f1878c4e8b5f27f396d974a22b6a307fc5f571a4d630ad1c294",
    "empty/activity.html": "0b4d465da8aadc19cdadb72d8ff21e472f54b3c320992ceade47d101801a0177",
    "empty/efficiency.html": "f83296e1d7e62946b4d594e82186a97f847c915f3bcab0b24fe694ee86e02b36",
    "empty/health.html": "ba12cead235b5d5a252770f03c40179fbe3e84ba2678aa0092bf66d2b417d4b8",
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
