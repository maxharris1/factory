from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import settings
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "settings.CSS": "6d0eef86f486be26e2fe58a99497b5f8f60b6e5a56522281d24c534365962fb6",
    "settings.SCRIPT": "0e715a5132b9b4166c6ee7e1d4daa78654bb9d4c08bcd1fc40a22a449bba75f4",
    "seeded/main": "3a1313d5b46a7078dad7d097da3928c90f878ed1b5c91a472aa8b49aec4003ad",
    "seeded/chrome": "5aaba2cf343d049455b3b6abb104ebf9345e002ad6c1e3bbe9199f8fe1e39244",
    "hostile/main": "3a1313d5b46a7078dad7d097da3928c90f878ed1b5c91a472aa8b49aec4003ad",
    "hostile/chrome": "98a582c4ed7091f11a68c4b9253c527c81684929716337e56a851500454fd79f",
    "empty/main": "3a1313d5b46a7078dad7d097da3928c90f878ed1b5c91a472aa8b49aec4003ad",
    "empty/chrome": "0570b79f92e2c6922e44ba0c292a432cf068f123f91ac8a03dbd451c65e8ad36",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {"settings.CSS": settings.CSS, "settings.SCRIPT": settings.SCRIPT}, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
