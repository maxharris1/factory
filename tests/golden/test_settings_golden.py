from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import settings
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "settings.CSS": "6d28070b6f594121087183618668c453fadcae7644fa256dd493bce321f334c9",
    "settings.SCRIPT": "b783b598b1a2b82dffbe11adcfd329d0637a33b5a22fab1ae2bac53628d5cb2d",
    "seeded/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "seeded/chrome": "ab6929423a5f07fb1c4a8a7d80c2a6c1127de6fadf19a3b15686f7c1c3e72780",
    "hostile/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "hostile/chrome": "2e7e081c86450214e0d7ddcc19077fab9bf272d732d855bd787e9f12754f29a6",
    "empty/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "empty/chrome": "af962c3d99d7e03cd2124b2ff32ed586298bf6712a2c277794978a7dd9380032",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {"settings.CSS": settings.CSS, "settings.SCRIPT": settings.SCRIPT}, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
