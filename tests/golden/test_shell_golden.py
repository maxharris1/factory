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
    PAGE_FILES, record, render, served, sha, snapshots, split, substitutions,
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
    "shell.CSS": "0f571bfddc9278f822ab38ef198c7a59fb80de3b584e1ad93a6f2fe10465b6ac",
    "common.CSS_PANEL": "67ebe04be58199ee0edf694380f00dc44e76148bb453d0f80f99d2db04bd0cae",
    "common.CSS_PARTS": "9d35db7fe3d3aa48a5c998d48ae3feccf78825131c3793336ffc7968710dbfdf",
    "shell.CSS_NAV": "6efc6d8b65ddcb1524575eb0573e6f6271259f2a5ffdba228639e9b75712cc5b",
    "common.CSS_MISC": "3ef1afa5b09c2e89dcdd3faca4dd5a2fdbe9a5de738f40e9b3966bff640849d5",
    "shell.CSS_PX": "66e50adc3d7ac0f6d3d17fe47305b9b533ebb810d1b8e535fd8c89ff9cd095e0",
    "common.CSS_TAIL": "adfd6e186608e777c9bd918b8107ab891cc46d5410acfd3b078d8d9bb0c622a2",
    "common.SCRIPT_HEAD": "4fc444aee603fab35c7d0d74733ed84fc2e5f524be3a97400f9f3ebc291b0c17",
    "shell.SCRIPT": "c6f5f260094a35b1a241b0f99a198d116f84b8a20a334419354b3517f58c5b63",
    "common.SCRIPT_TELEMETRY": "7d2a9166e208474d94a535dbbd9fbe8f2d78ad33591f287685a38bb0814bd34f",
    "common.SCRIPT_TAIL": "d0d395510a12c0da47f2458f63bd2b0a43627a26996b8703bb2262b9f12fac6f",
    "PX_SCRIPT": "777ae1a8b65aa0d735a696c3e6ba885f3e5e086f61c26784d9e5efcb5bc5bef5",
    "__PIXELSTRIP__": "e1e4d8a1daef95f2cd9c872655caa5e963837af7bf6e409fbb771c38bc65fcfc",
    "__SHELL__": "64254ccf3c3532c0494fcc6af489cca2abd6b66d4cc38c6ce50513258a795a3d",
    "__POLL_MS__": "9f69998560dcfd8016442e0a32e959191df095817a164ce844c64ec5a8b0cc1b",
    "__FONTS__": "c0fa48bc2983db78f449c669c8b225684b4b09de6cfcab610d8eb98a9463cfd6",
    "seeded/index.html/chrome": "0a8ffabd85bf57b1c7dea9a499df231e862f995d29134036edbcbdc286125e3b",
    "seeded/job.html/chrome": "0eb603610142b4c6d93f15c6076bb69b60dec58b6429c64fb6d7c7074d7ad83f",
    "seeded/activity.html/chrome": "ca00a1fa1253bba2f40a75827c4b893c8906d4e236e54920a1d611ce6d35b5f3",
    "seeded/efficiency.html/chrome": "c26f7b3a41c71644f96b33d8384494a9fe81dd78db216aed69a278b8655f71bc",
    "seeded/health.html/chrome": "87aa45406cfb21e3a8b55b80a8449a67fdc8cb50931240d8c583eac2d4338dd6",
    "hostile/index.html/chrome": "5a734816fa9563c8f243b29fb5c071cd4bbebf4e90d47e8c1adeaf268ec65799",
    "hostile/job.html/chrome": "85226d27bc97bf6944c88f6af626bf3495c9ad6a79c1b5ee9a2434a7559b435b",
    "hostile/activity.html/chrome": "8ee30fef2e5d928b38a300fa9f58d374aa5f08c7032faea0ef02bca188698231",
    "hostile/efficiency.html/chrome": "e76657c6e62954fdaa26519a0dfe79b203e040cf187fbe23ae5bfb6b2921ae0e",
    "hostile/health.html/chrome": "f998539f4f098ea9855764ebdfb8d8d1b54a4cc426ca421f260ba7455b96f41b",
    "empty/index.html/chrome": "5874f90e87b3ee61e41b0649b02623df2b1d87590d31b29bb1efe68f889b2f9f",
    "empty/job.html/chrome": "78c6b2bfe6a8a3a39cdbe5dcbce2670b5eb730f502d2a3a8cc409d937d955ef7",
    "empty/activity.html/chrome": "94fd064fe383db5bd30b7ffae462111f385939940ab21804f6fb59170cd9c6bd",
    "empty/efficiency.html/chrome": "0e87ae7051e3c1a551a9309768b9f3b189c3becfdd4d821a5b6fa4f4b6feb9d5",
    "empty/health.html/chrome": "154d0c1de17b6110f3401c6a987db977e8f152fb96902d5c94308e9ecfd4c03a",
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
    for case, pages in served(root).items():
        for name, html in pages.items():
            found[f"{case}/{name}/chrome"] = sha(split(html)["chrome"])
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
