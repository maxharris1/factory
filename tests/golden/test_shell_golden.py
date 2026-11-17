from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest

import lib.dashboard_page as page
from lib.dashboard_flow import load_chart
from lib.dashboard_pages import (
    activity, common, efficiency, lanes, shell, system, ticket_detail, tickets,
)
from tests.golden.pages import (
    PAGE_FILES, record, render, sha, snapshots, split, substitutions,
)

FACADE = Path(__file__).resolve().parents[2] / "lib" / "dashboard_page.py"

CSS_ORDER = (
    (common, "CSS_BASE"), (shell, "CSS"), (common, "CSS_PANEL"), (lanes, "CSS"),
    (common, "CSS_PARTS"), (shell, "CSS_NAV"), (system, "CSS"), (ticket_detail, "CSS"),
    (tickets, "CSS"), (common, "CSS_MISC"), (efficiency, "CSS"), (activity, "CSS"),
    (shell, "CSS_PX"), (common, "CSS_TAIL"),
)
SCRIPT_ORDER = (
    (common, "SCRIPT_HEAD"), (shell, "SCRIPT"), (lanes, "SCRIPT"), (activity, "SCRIPT"),
    (tickets, "SCRIPT_TICKETS"), (system, "SCRIPT_LOG"), (system, "SCRIPT_HEALTH"),
    (efficiency, "SCRIPT"), (ticket_detail, "SCRIPT"), (common, "SCRIPT_TELEMETRY"),
    (system, "SCRIPT_COMPONENTS"), (common, "SCRIPT_TAIL"),
)
SHELL_SUBSTITUTIONS = ("__PIXELSTRIP__", "__SHELL__", "__POLL_MS__", "__FONTS__")

GOLDEN = {
    "common.CSS_BASE": "5e6d3ad7f1bc8adea16e76e27445f851e3a0250229682a04b3d6297e0d322d10",
    "shell.CSS": "594dfd0f73432b0378f37be48b33f8d8e61216ade7aab30331f2d6f4cc115513",
    "common.CSS_PANEL": "6c88da11fafc1623c83ef0e31c2139a3549bd8adc3ed9f967e8ff2859af81850",
    "common.CSS_PARTS": "9d35db7fe3d3aa48a5c998d48ae3feccf78825131c3793336ffc7968710dbfdf",
    "shell.CSS_NAV": "6efc6d8b65ddcb1524575eb0573e6f6271259f2a5ffdba228639e9b75712cc5b",
    "common.CSS_MISC": "3ef1afa5b09c2e89dcdd3faca4dd5a2fdbe9a5de738f40e9b3966bff640849d5",
    "shell.CSS_PX": "66e50adc3d7ac0f6d3d17fe47305b9b533ebb810d1b8e535fd8c89ff9cd095e0",
    "common.CSS_TAIL": "adfd6e186608e777c9bd918b8107ab891cc46d5410acfd3b078d8d9bb0c622a2",
    "common.SCRIPT_HEAD": "334b10c888d17358d0133ad470004acd0aab8eef6990ce12085d129c5dd0f902",
    "shell.SCRIPT": "539c0fa7d851db0b0e2d92d92513b67eab4918de5a127770e2326891a272af06",
    "common.SCRIPT_TELEMETRY": "7d2a9166e208474d94a535dbbd9fbe8f2d78ad33591f287685a38bb0814bd34f",
    "common.SCRIPT_TAIL": "2eab52cb9f83be2a6a169b45c4713073b1b8906c996de95f34e92e23c206c004",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "__PIXELSTRIP__": "5eec0f40428daccb97fb1763e5316bd900c435f1f6535d3f7cf0b814847cd065",
    "__SHELL__": "33e9dfdaec78d0331264fa9b4e2838ac01512ee17f12e0c7c6513791bde3d712",
    "__POLL_MS__": "9f69998560dcfd8016442e0a32e959191df095817a164ce844c64ec5a8b0cc1b",
    "__FONTS__": "c0fa48bc2983db78f449c669c8b225684b4b09de6cfcab610d8eb98a9463cfd6",
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


def key(module: ModuleType, attr: str) -> str:
    return f"{module.__name__.rsplit('.', 1)[1]}.{attr}"


def digests(root: Path) -> dict[str, str]:
    found = {
        key(module, attr): sha(getattr(module, attr))
        for module, attr in CSS_ORDER + SCRIPT_ORDER if module in (common, shell)
    }
    found["PX_SCRIPT"] = sha(page.PX_SCRIPT)
    values = substitutions(None)
    found.update({name: sha(values[name]) for name in SHELL_SUBSTITUTIONS})
    return found


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[dict, dict[str, str]]]:
    found = snapshots(tmp_path_factory.mktemp("site"))
    return {case: (snapshot, render(snapshot)) for case, snapshot in found.items()}


def test_the_shell_and_chrome_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


def test_css_and_script_join_the_parts_in_served_order() -> None:
    assert page.CSS == "".join(getattr(module, attr) for module, attr in CSS_ORDER)
    assert page.SCRIPT == "".join(getattr(module, attr) for module, attr in SCRIPT_ORDER)


def test_every_part_and_substitution_is_pinned_by_a_golden_file() -> None:
    pinned = set(GOLDEN)
    for stem in PAGE_FILES.values():
        pinned |= set(importlib.import_module(f"tests.golden.{stem}").GOLDEN)
    owned = {key(module, attr) for module, attr in CSS_ORDER + SCRIPT_ORDER}
    owned |= set(substitutions(None)) | {"TELEMETRY_CSS", "PX_SCRIPT"}
    assert owned <= pinned


def test_each_page_pins_its_rendered_digests_in_its_own_file(site) -> None:
    assert not [key for key in GOLDEN for case in site if key.startswith(f"{case}/")]
    for stem in PAGE_FILES.values():
        golden = importlib.import_module(f"tests.golden.{stem}").GOLDEN
        for case in site:
            assert {f"{case}/main", f"{case}/chrome"} <= set(golden), (stem, case)


def test_each_placeholder_occurs_in_the_script_once() -> None:
    for placeholder in substitutions(None):
        assert page.SCRIPT.count(placeholder) == 1, placeholder


def test_page_script_fills_the_placeholders_in_order(site) -> None:
    charts = [load_chart()] + [snapshot.get("statechart") for snapshot, _ in site.values()]
    for chart in charts:
        expected = page.SCRIPT
        for placeholder, value in substitutions(chart).items():
            expected = expected.replace(placeholder, value)
        assert page.page_script(chart) == expected


def test_every_page_inlines_the_css_and_its_page_script(site) -> None:
    for snapshot, pages in site.values():
        script = page.page_script(snapshot.get("statechart"))
        for name, html in pages.items():
            parts = split(html)
            assert parts["style"] == page.CSS + page.TELEMETRY_CSS, name
            assert parts["script"] == script, name


def test_every_page_has_one_golden_file() -> None:
    assert set(PAGE_FILES) == set(page.PAGES)
    assert len(set(PAGE_FILES.values())) == len(PAGE_FILES)
    for stem in PAGE_FILES.values():
        assert (Path(__file__).parent / f"{stem}.py").is_file(), stem


@pytest.mark.parametrize("name", NAMES)
def test_the_facade_still_exports(name: str) -> None:
    assert hasattr(page, name)


def test_the_facade_is_under_150_lines() -> None:
    assert len(FACADE.read_text().splitlines()) < 150


if __name__ == "__main__":
    record(digests)
