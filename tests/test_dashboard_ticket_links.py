"""#656: every ticket link opens ticket.html#t=<repo>#<ticket>, the Tickets board draws no
job detail, and an old job.html#job-<id> address moves to its ticket page."""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

from lib.dashboard import render_pages
from lib.dashboard_pages.activity import relay_threads
from lib.dashboard_pages.common import anchor, ticket_address, ticket_page_href
from tests.test_dashboard_activity import activity_snapshot
from tests.test_dashboard_layout import BAR, JOB, _no_script, _run_page, _section, _snapshot, needs_node
from tests.test_dashboard_system import CHROME, _Chrome
from tests.test_dashboard_ticket_detail import _open, _serve, _wait

TAG = re.compile(r'<([a-z][a-z0-9]*)((?:\s+[a-z-]+(?:="[^"]*")?)*)\s*/?>')
ATTR = re.compile(r'([a-z-]+)="([^"]*)"')


def _tags(markup: str) -> list[tuple[str, dict[str, str]]]:
    return [(name, {k: html.unescape(v) for k, v in ATTR.findall(attrs)}) for name, attrs in TAG.findall(markup)]


def _keyed_links(markup: str, key_attr: str, cls: str | None) -> list[tuple[str, str]]:
    """(key, href) of each <a> of class `cls`, keyed by the nearest `key_attr` at or before it."""
    key, found = None, []
    for name, attrs in _tags(markup):
        key = attrs.get(key_attr, key)
        if name == "a" and (cls is None or cls in attrs.get("class", "").split()):
            found.append((key, attrs["href"]))
    return found


def _active(snapshot: dict) -> set[str]:
    return {ticket_address(row).lower() for kind in ("running", "queued") for row in snapshot[kind]}


def _row(address: str) -> dict:
    repo, _, ticket_id = address.partition("#")
    return {"repo": repo, "ticket_id": ticket_id}


def _renders(tmp_path: Path, snapshot: dict, page: str, panel: str) -> dict[str, str]:
    name = {"main": "index.html"}.get(page, f"{page}.html")
    out = _run_page(tmp_path, snapshot, page, BAR + [panel])
    return {"server": _section(_no_script(render_pages(snapshot)[name]), panel), "script": out["html"][panel]}


@needs_node
def test_lanes_tiles_and_queue_rows_link_to_their_ticket_page(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    for render, markup in _renders(tmp_path, snapshot, "main", "lanes").items():
        tiles = _keyed_links(markup, "title", "tile")
        assert [href for _, href in tiles] == [ticket_page_href(row) for row in snapshot["running"]], render
        queue = [href for lane, href in _keyed_links(markup, "data-device", None) if lane == "queue"]
        assert queue == [ticket_page_href(row) for row in snapshot["queued"]], render


@needs_node
def test_every_board_card_links_to_its_ticket_page_with_or_without_a_job(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    for render, markup in _renders(tmp_path, snapshot, "job", "pipeline").items():
        cards = [(key, href) for key, href in _keyed_links(markup, "data-ticket", None) if "github.com" not in href]
        assert cards and all(href == ticket_page_href(_row(key)) for key, href in cards), (render, cards)
        assert {key.lower() for key, _ in cards} - _active(snapshot), f"{render}: no card without a job"
        assert 'class="tk-tid"' not in markup, render


@needs_node
def test_relay_ticket_ids_link_to_their_ticket_page(tmp_path: Path) -> None:
    snapshot = activity_snapshot(tmp_path)
    want = {t["key"]: ticket_page_href(t["ref"]) for t in relay_threads(snapshot)}
    assert set(want) - _active(snapshot), "the fixture needs a relay thread with no active job"
    for render, markup in _renders(tmp_path, snapshot, "activity", "relay").items():
        assert dict(_keyed_links(markup, "data-thread", "rl-tk")) == want, render


@needs_node
def test_open_ticket_in_needs_attention_links_to_the_ticket_page(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    tickets = [row for row in snapshot["attention"]["items"] if not row.get("component")]
    want = {f"{row['kind']}:{ticket_address(row)}": ticket_page_href(row) for row in tickets}
    assert {ticket_address(row).lower() for row in tickets} - _active(snapshot), "the fixture needs a ticket with no job"
    for render, markup in _renders(tmp_path, snapshot, "activity", "attention").items():
        links = {key: href for key, href in _keyed_links(markup, "data-attn", "at-go") if key in want}
        assert links == want, render


@needs_node
@pytest.mark.parametrize("page", ["main", "job", "efficiency", "health"])
def test_attention_strip_ticket_items_link_to_the_ticket_page(tmp_path: Path, page: str) -> None:
    snapshot = _snapshot(tmp_path)
    tickets = {f"{row['kind']}:{ticket_address(row)}": row for row in snapshot["attention"]["items"] if not row.get("component")}
    for render, markup in _renders(tmp_path, snapshot, page, "attention-strip").items():
        links = [(key, href) for key, href in _keyed_links(markup, "data-attn", "attn-who") if key in tickets]
        assert links, render
        assert all(href == ticket_page_href(tickets[key]) for key, href in links), (render, links)
        assert {key.partition(":")[2].lower() for key, _ in links} - _active(snapshot), f"{render}: no item without a job"


@needs_node
def test_no_page_links_to_an_old_job_address(tmp_path: Path) -> None:
    for name, snapshot in (("layout", _snapshot(tmp_path / "a")), ("activity", activity_snapshot(tmp_path / "b"))):
        for file, markup in render_pages(snapshot).items():
            page = {"index.html": "main"}.get(file, file.removesuffix(".html"))
            out = _run_page(tmp_path / name / page, snapshot, page, re.findall(r'\bid="([^"]+)"', markup))
            for render, text in (("server", markup), ("script", "".join(out["html"].values()))):
                assert "job.html#job-" not in text, (name, file, render)


def _job_page(tmp_path: Path, snapshot: dict, hash_: str) -> dict:
    return _run_page(tmp_path, snapshot, "job", JOB + ["jobview"], hash_=hash_)


@needs_node
@pytest.mark.parametrize("hash_", ["", "#job-h-review"])
def test_the_board_draws_no_job_detail(tmp_path: Path, hash_: str) -> None:
    snapshot = _snapshot(tmp_path)
    server = render_pages(snapshot)["job.html"]
    script = "".join(_job_page(tmp_path, snapshot, hash_)["html"].values())
    for render, markup in (("server", server), ("script", script)):
        assert 'id="jobview"' not in markup and 'class="jobpage' not in markup, render
        assert 'id="job-' not in markup, render


@needs_node
@pytest.mark.parametrize("job", ["h-review", "q-claude"])
def test_an_old_job_address_moves_to_its_ticket_page(tmp_path: Path, job: str) -> None:
    snapshot = _snapshot(tmp_path)
    (row,) = [r for kind in ("running", "queued") for r in snapshot[kind] if r["job"] == job]
    out = _job_page(tmp_path, snapshot, "#" + anchor(job))
    assert out["replaced"] == [ticket_page_href(row)]


@needs_node
@pytest.mark.parametrize("hash_", ["#job-finished", "#t=maxharris1%2Fcleffy%2340", "#pipeline", ""])
def test_any_other_address_stays_on_the_board(tmp_path: Path, hash_: str) -> None:
    out = _job_page(tmp_path, _snapshot(tmp_path), hash_)
    assert out["replaced"] == []
    assert 'class="tk-card' in out["html"]["pipeline"]


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
def test_an_old_job_link_lands_on_the_ticket_page_in_a_browser(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    row = next(r for r in snapshot["running"] if r["job"] == "h-review")
    server = _serve(tmp_path, {"site": snapshot})
    chrome = _Chrome(tmp_path / "profile")
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}/site/"
        _open(chrome, base + "job.html#job-h-review", "location.pathname.endsWith('/ticket.html')")
        _wait(chrome, "document.querySelector('#ticketview article.ticketpage')")
        landed = chrome.run("[location.pathname + location.hash, "
                            "document.querySelector('#ticketview article.ticketpage').dataset.ticket]")
    finally:
        chrome.close()
        server.shutdown()
    assert landed == ["/site/" + ticket_page_href(row), ticket_address(row)]
