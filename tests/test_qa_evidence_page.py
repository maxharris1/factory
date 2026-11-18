from __future__ import annotations

import json
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import pytest

from lib.dashboard_pages.qa_evidence import run_page
from lib.qa_evidence import collect
from lib.qa_storage import BUNDLE_NAME, RESULT_NAME, lane_prefix
from tests.test_qa_evidence import REPO, RUN, TICKET, FakeStorage, _bundle, _file

PNG = b"\x89PNG\r\n\x1a\n fake screenshot"
WEBM = b"\x1aE\xdf\xa3 fake video"
DOM = b"<!doctype html><script>alert(1)</script>"
EVIDENCE = {"shot.png": PNG, "flow.webm": WEBM, "dom.html": DOM}
LANES = {1: {"login": "pass", "cart #2": "fail"}, 2: {"search": "pass", "profile": "blocked"}}
MISSING = "evidence recycled or missing"
PAGE_URL = f"http://dashboard/qa/runs/{RUN}"


def collect_run(root: Path) -> Path:
    objects = {}
    for lane, scenarios in LANES.items():
        result = {
            "run_id": RUN, "lane": lane, "commit": "c0ffee", "status": "fail",
            "scenarios": [{"id": scenario, "result": outcome} for scenario, outcome in scenarios.items()],
        }
        members = [_file(f"{scenario}/{name}", data) for scenario in scenarios for name, data in EVIDENCE.items()]
        prefix = lane_prefix(REPO, RUN, lane)
        objects[prefix + RESULT_NAME] = json.dumps(result).encode()
        objects[prefix + BUNDLE_NAME] = _bundle(*members, _file("app.log", b"booting\n"))
    storage = FakeStorage(objects)
    for lane in LANES:
        assert collect(RUN, lane, storage=storage, repo=REPO, ticket=TICKET, root=root) is not None
    return root / "acme" / "shop" / TICKET / RUN


class Page(HTMLParser):
    def __init__(self, markup: str) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.chunks: list[str] = []
        self.feed(markup)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.links += [(tag, value or "") for name, value in attrs if name in ("src", "href")]

    def handle_data(self, data: str) -> None:
        self.chunks.append(data)

    @property
    def text(self) -> str:
        return " ".join(self.chunks)


def linked_files(page: Page, run: Path) -> set[tuple[str, str]]:
    found = set()
    for tag, link in page.links:
        assert not link.startswith("/") and not urlsplit(link).scheme, link
        resolved = urlsplit(urljoin(PAGE_URL, link))
        assert resolved.path.startswith("/qa/runs/") and not resolved.query and not resolved.fragment, link
        target = run.parent.joinpath(*(unquote(part) for part in resolved.path.split("/")[3:]))
        assert target.resolve().is_relative_to(run.resolve()), link
        assert target.is_file(), f"broken link {link}"
        found.add((tag, target.relative_to(run).as_posix()))
    return found


def expected_links(lanes: dict[int, dict[str, str]]) -> set[tuple[str, str]]:
    tags = {"shot.png": "img", "flow.webm": "video", "dom.html": "a"}
    return {
        (tags[name], f"lane-{lane}/{scenario}/{name}")
        for lane, scenarios in lanes.items() for scenario in scenarios for name in EVIDENCE
    }


@pytest.fixture
def run(tmp_path: Path) -> Path:
    return collect_run(tmp_path / "evidence")


def test_page_names_every_scenario_and_links_its_evidence(run: Path) -> None:
    page = Page(run_page(run, RUN))

    for scenarios in LANES.values():
        for scenario, outcome in scenarios.items():
            assert scenario in page.text and outcome in page.text
    assert linked_files(page, run) == expected_links(LANES)
    assert MISSING not in page.text
    assert "script" not in page.tags


def _rewrite_manifest(lane: Path, paths: list[object]) -> None:
    (lane / "manifest.json").write_text(json.dumps({"files": [{"path": path} for path in paths]}))


DAMAGE: dict[str, Callable[[Path], object]] = {
    "result deleted": lambda lane: (lane / RESULT_NAME).unlink(),
    "manifest corrupted": lambda lane: (lane / "manifest.json").write_text("{not json"),
    "manifest wrong shape": lambda lane: _rewrite_manifest(lane, [7]),
    "result wrong shape": lambda lane: (lane / RESULT_NAME).write_text('{"scenarios": {"id": "x"}}'),
}


@pytest.mark.parametrize("damage", DAMAGE.values(), ids=DAMAGE.keys())
def test_an_unreadable_lane_reads_as_recycled(run: Path, damage: Callable[[Path], object]) -> None:
    damage(run / "lane-2")
    page = Page(run_page(run, RUN))

    assert page.text.count(MISSING) == 1
    assert linked_files(page, run) == expected_links({1: LANES[1]})
    assert "login" in page.text and "search" not in page.text


@pytest.mark.parametrize("listed", [
    "search/shot.png",
    "search/./shot.png",
    "search/../search/shot.png",
    "search//shot.png",
])
def test_a_listed_file_not_on_disk_or_unsafe_reads_as_recycled(run: Path, listed: str) -> None:
    lane = run / "lane-2"
    if listed == "search/shot.png":
        (lane / listed).unlink()
    else:
        manifest = json.loads((lane / "manifest.json").read_text())
        _rewrite_manifest(lane, [entry["path"] for entry in manifest["files"] if entry["path"] != "search/shot.png"]
                          + [listed])
    page = Page(run_page(run, RUN))

    assert page.text.count(MISSING) == 1
    assert linked_files(page, run) == expected_links(LANES) - {("img", "lane-2/search/shot.png")}


@pytest.mark.parametrize("exists", [False, True])
def test_a_run_with_no_lanes_reads_as_recycled_once(tmp_path: Path, exists: bool) -> None:
    folder = tmp_path / RUN
    if exists:
        folder.mkdir()
    page = Page(run_page(folder, RUN))

    assert page.text.count(MISSING) == 1
    assert page.links == []


def test_lanes_are_in_numeric_order(tmp_path: Path) -> None:
    for name in ("lane-10", "lane-2", "lane-02", "notes"):
        (tmp_path / name).mkdir()
    text = Page(run_page(tmp_path, RUN)).text

    assert text.count(MISSING) == 2
    assert text.index("Lane 2") < text.index("Lane 10")
    assert "02" not in text and "notes" not in text


def test_untrusted_text_is_escaped(run: Path) -> None:
    hostile = "<script>alert(1)</script>"
    lane = run / "lane-1"
    (lane / RESULT_NAME).write_text(json.dumps({"scenarios": [{"id": hostile, "result": "<b>fail</b>"}]}))
    markup = run_page(run, RUN)
    page = Page(markup)

    assert hostile in page.text and "<b>fail</b>" in page.text
    assert "script" not in page.tags and "b" not in page.tags
    assert hostile not in markup
