from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import activity
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "activity.CSS": "51ad4744666673c5e668ad042e11e65f48ace480ff85991b4436a256ba57e6f9",
    "activity.SCRIPT": "06664951616bf8def28a5630e7ddab5dd2f1194115069e5ca72155f82121b9aa",
    "seeded/main": "d0fcff172911a1d87399ad0b6b83791c1c71fdb8fafb31e8f198ddc35ae3f280",
    "seeded/chrome": "8bb0b2d25b62fd9f57078928f0de45c84b44d0d4abc7976c997b46bf9f168bb7",
    "hostile/main": "81ed6c31be92f082a69a69f25504cb2823b89d987324b7fbcee14a6f1817b59b",
    "hostile/chrome": "1552f8c25f2ca83f930cf58660a86f5646b51c0178ed9d02841e9e5aa8c736b1",
    "empty/main": "5ae29a1d33e19d8e1327d638b05372532e97d29662bbc2d5db039d1fa5882e12",
    "empty/chrome": "4da2d0bf5812c623a819ffc8a1d860a597c6b242473a3deee3c57068275d7591",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {
        "activity.CSS": activity.CSS,
        "activity.SCRIPT": activity.SCRIPT,
    }, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
