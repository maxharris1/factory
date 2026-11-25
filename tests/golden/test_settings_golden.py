from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import settings
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "settings.CSS": "12d7206cc8f5257a9575f5a62275fd61c8883b9e41dd2b66b66edc02da29f978",
    "settings.SCRIPT": "7726b02f45a13e99ddfa0f4795e696db28d80f21f90d9aa5401921d5eec1df34",
    "seeded/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "seeded/chrome": "29649599295a55b7a0877d374b3337d081ede20bf300a8decc9eb039ac7cc52a",
    "hostile/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "hostile/chrome": "73db73c5b269cf74979781d511b29c6c8213c1fabf0197829a2aafebee2d2afa",
    "empty/main": "5fcd6365b9b111a4793a9885a6a5590189bbd2bc081bcfb209426850758f87d3",
    "empty/chrome": "3b64a05f2346f05528c5d6620d25423eb050a331108c396da18615293a7b0cb8",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {"settings.CSS": settings.CSS, "settings.SCRIPT": settings.SCRIPT}, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
