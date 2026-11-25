from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from tests import test_dashboard_attention_actions as attention
from tests import test_dashboard_system as system
from tests import test_dashboard_ticket_detail as ticket_detail
from tests import test_dashboard_tickets_board as board
from tests.test_dashboard import snapshot_for

ASSET = re.compile(r'(?:href|src)="(/[^"]+\.(?:css|js))"')


class Served(Exception):
    def __init__(self, handler) -> None:
        self.handler = handler


def _capture(_address, handler):
    raise Served(handler)


def _status(handler, path: str) -> int:
    request = object.__new__(handler.func)
    request.directory, request.path = handler.keywords["directory"], path
    request.command, request.request_version, request.headers = "GET", "HTTP/1.1", {}
    codes: list[int] = []
    request.send_response = request.send_error = lambda code, *args: codes.append(code)
    request.send_header = request.end_headers = lambda *args: None
    body = request.send_head()
    if body is not None:
        body.close()
    return codes[0]


def _served_by(start) -> dict[str, int]:
    with patch("http.server.ThreadingHTTPServer", _capture), pytest.raises(Served) as served:
        start()
    handler = served.value.handler
    root = Path(handler.keywords["directory"])
    urls = {url for page in root.rglob("*.html") for url in ASSET.findall(page.read_text(encoding="utf-8"))}
    assert urls, "the served pages load no stylesheet or script"
    return {url: _status(handler, url) for url in sorted(urls)}


def _system(tmp_path: Path) -> None:
    with system._browse(tmp_path, snapshot_for(tmp_path)):
        pass


def _attention(tmp_path: Path) -> None:
    with attention._browse(tmp_path, snapshot_for(tmp_path), None):
        pass


def _ticket_detail(tmp_path: Path) -> None:
    ticket_detail._serve(tmp_path, {"first": snapshot_for(tmp_path / "a"), "second": snapshot_for(tmp_path / "b")})


def _board(tmp_path: Path) -> None:
    with patch("tests.test_dashboard_system.CHROME", "chrome"):
        board.test_the_first_render_keeps_a_keyboard_selection_in_the_find_box(tmp_path, snapshot_for(tmp_path))


@pytest.mark.parametrize("start", [_system, _attention, _ticket_detail, _board])
def test_every_asset_a_served_page_loads_is_served(tmp_path: Path, start) -> None:
    served = _served_by(lambda: start(tmp_path))
    assert all(code == 200 for code in served.values()), served


def test_sites_with_different_statecharts_cannot_share_one_root_script(tmp_path: Path) -> None:
    other = snapshot_for(tmp_path / "b")
    other["statechart"] = None
    with pytest.raises(AssertionError, match="statechart"):
        ticket_detail._serve(tmp_path, {"first": snapshot_for(tmp_path / "a"), "second": other})
