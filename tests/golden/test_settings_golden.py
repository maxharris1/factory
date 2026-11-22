from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import settings
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "settings.CSS": "180647273b14cf6338ec209216f48f8e1db3d6584ec8a07ee8bf612c80a75134",
    "settings.SCRIPT": "3a43c07a3f6cad949414666e85b1eab12d07b3729e077eddf7d4b193040d76df",
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
