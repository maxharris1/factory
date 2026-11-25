from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import settings
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "settings.CSS": "12d7206cc8f5257a9575f5a62275fd61c8883b9e41dd2b66b66edc02da29f978",
    "settings.SCRIPT": "7726b02f45a13e99ddfa0f4795e696db28d80f21f90d9aa5401921d5eec1df34",
    "seeded/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "seeded/chrome": "a30c252f2d59b9d99badc42e4a9a21c5a3aa87ca9e387b5be3c87fafff01f717",
    "hostile/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "hostile/chrome": "455bc3a734f48962441ae3733123c71c964c78849810774df87c2b4017f14da8",
    "empty/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "empty/chrome": "43b071bcb67a41de3a078efbeabcd9b7e0cee6fd0b7b87e1ca09cef007ab6012",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {"settings.CSS": settings.CSS, "settings.SCRIPT": settings.SCRIPT}, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
