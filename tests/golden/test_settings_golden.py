from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import settings
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "settings.CSS": "6d28070b6f594121087183618668c453fadcae7644fa256dd493bce321f334c9",
    "settings.SCRIPT": "db9696e85b0cec45e6a8ae6fb39125daeba4798acc49a3805fbc2895631e56da",
    "seeded/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "seeded/chrome": "2eaa09bc5a0e8dca86e8fca5becc26fb962999496985824555fdeb173236b050",
    "hostile/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "hostile/chrome": "5e76145cfa5575068199a74c288299e13a4618403bbcaf59258d74dc3cd51cb9",
    "empty/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "empty/chrome": "f8e2b2ab3db9a261f6abf827dfb52aa17afa9754c2db88748cde22b26c5adac5",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {"settings.CSS": settings.CSS, "settings.SCRIPT": settings.SCRIPT}, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
