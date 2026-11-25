from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import settings
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "settings.CSS": "6d28070b6f594121087183618668c453fadcae7644fa256dd493bce321f334c9",
    "settings.SCRIPT": "168e13aea34922d2e28a52dde6cdfbc031e060a645ae81caede83e40c318fb8c",
    "seeded/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "seeded/chrome": "5aaba2cf343d049455b3b6abb104ebf9345e002ad6c1e3bbe9199f8fe1e39244",
    "hostile/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "hostile/chrome": "98a582c4ed7091f11a68c4b9253c527c81684929716337e56a851500454fd79f",
    "empty/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "empty/chrome": "0570b79f92e2c6922e44ba0c292a432cf068f123f91ac8a03dbd451c65e8ad36",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {"settings.CSS": settings.CSS, "settings.SCRIPT": settings.SCRIPT}, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
