from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import settings
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "settings.CSS": "6d28070b6f594121087183618668c453fadcae7644fa256dd493bce321f334c9",
    "settings.SCRIPT": "d46f60efba23c4a043d1eadd1810faf435e72c8beeecbdc519f3ab9d77c7a3dc",
    "seeded/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "seeded/chrome": "fff684a237ac0adfe9a9ec5fa3d43d8ccea303543b6410be80e523284394a16c",
    "hostile/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "hostile/chrome": "eccfa2aebb8198ca25f5e8ba098d6c55ebb1c10fb20f6386c24cf8febdc8a41b",
    "empty/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "empty/chrome": "239792899b7d616bed2176a9852c1f38d33c450436c0e9b97504a84dbcfeda1c",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {"settings.CSS": settings.CSS, "settings.SCRIPT": settings.SCRIPT}, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
