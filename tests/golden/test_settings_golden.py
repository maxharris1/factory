from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import settings
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "settings.CSS": "6d28070b6f594121087183618668c453fadcae7644fa256dd493bce321f334c9",
    "settings.SCRIPT": "db9696e85b0cec45e6a8ae6fb39125daeba4798acc49a3805fbc2895631e56da",
    "seeded/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "seeded/chrome": "c4f48dbfb205e2d0320599d921b528d0fd2bd45478ff4010b9a9233c344c5b12",
    "hostile/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "hostile/chrome": "2d0ebd9ffc8c56e23d24c8e5e433bce31914e65fc4966a802f583431ec409933",
    "empty/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "empty/chrome": "57e64f505d92c35ef075d2f9d788f9c6e1d687f1f3945295fe9e0c763785f1d9",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {"settings.CSS": settings.CSS, "settings.SCRIPT": settings.SCRIPT}, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
